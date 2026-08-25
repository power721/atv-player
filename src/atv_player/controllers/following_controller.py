# ruff: noqa: E501
from __future__ import annotations

import ast
import json
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from atv_player.ai.enrichment import FollowingDetailSummaryInput
from atv_player.danmaku.utils import infer_playlist_episode_number
from atv_player.episode_titles import extract_season_number
from atv_player.following_metadata import (
    FollowingMetadataGateway,
    _media_kind_category,
    _resolve_effective_media_kind,
    build_following_from_metadata_candidate,
    build_following_metadata_bundle,
    build_following_source_metadata_bundle,
    build_snapshot_from_record,
    compute_episode_counts,
    following_candidate_from_url,
    load_candidate_detail_record,
    load_candidate_detail_record_full,
    merge_following_snapshot,
)
from atv_player.following_models import (
    FollowingAISummary,
    FollowingCardItem,
    FollowingCompletionState,
    FollowingDetailSnapshot,
    FollowingEpisode,
    FollowingRecord,
    FollowingSourceBinding,
    compare_progress,
    format_progress_episode,
    progress_at_or_beyond,
    provider_priority_for_media_kind,
    resolve_display_total_episodes,
    resolve_following_completion_state,
    resolve_latest_released_season_number,
    resolve_new_episode_count,
    resolve_progress_season,
)
from atv_player.metadata.discovery import (
    DiscoveryItem,
    DiscoveryQuery,
    DiscoveryResult,
    RecommendationSeed,
)
from atv_player.metadata.models import MetadataQuery, MetadataRecord
from atv_player.metadata.scrape import MetadataScrapeCandidate
from atv_player.models import PlayItem, VodItem


@dataclass(slots=True)
class FollowingDetailView:
    record: FollowingRecord
    snapshot: FollowingDetailSnapshot


class FollowingController:
    _RECOMMENDATION_SEED_LIMIT = 8

    def __init__(
        self,
        repository,
        *,
        metadata_search_service,
        update_service=None,
        now=None,
        discovery_service=None,
        favorite_tmdb_binding_repository=None,
        ai_enrichment_service=None,
        backend_sync_service=None,
    ) -> None:
        self._repository = repository
        self._metadata_search_service = metadata_search_service
        self._update_service = update_service
        self._now = now or (lambda: int(time.time()))
        self._discovery_service = discovery_service
        self._favorite_tmdb_binding_repository = favorite_tmdb_binding_repository
        self._ai_enrichment_service = ai_enrichment_service
        self._backend_sync_service = backend_sync_service
        self._discovery_memory_cache: dict[str, DiscoveryResult] = {}

    def search_media(self, keyword: str, *, year: str = ""):
        url_candidate = self.candidate_from_url(keyword)
        if url_candidate is not None:
            from atv_player.metadata.scrape import MetadataScrapeGroup

            url_candidate = self._hydrate_url_candidate(url_candidate)
            return [
                MetadataScrapeGroup(
                    provider=url_candidate.provider,
                    provider_label=url_candidate.provider_label,
                    items=[url_candidate],
                )
            ]
        query = MetadataQuery(title=keyword.strip(), year=str(year or "").strip())
        groups = self._search_tmdb_following(query)
        return [self._sort_following_group_items(group) for group in groups]

    def has_discovery_tabs(self) -> bool:
        return self._discovery_service is not None

    def load_discovery_tab(self, tab_key: str, *, query: str = "", page: int = 1, filters: dict[str, str] | None = None):
        if self._discovery_service is None:
            raise RuntimeError("TMDB discovery unavailable")
        normalized_tab = str(tab_key or "").strip() or "recommendation"
        filters = dict(filters or {})
        cache_key = self._discovery_memory_cache_key(normalized_tab, page=page, filters=filters)
        if cache_key:
            cached = self._discovery_memory_cache.get(cache_key)
            if cached is not None:
                return self._clone_discovery_result(cached)
        if normalized_tab == "recommendation":
            result = self._load_recommendation_result(page=page)
            self._store_discovery_memory_cache(cache_key, result)
            return self._clone_discovery_result(result)
        if normalized_tab == "search":
            groups = self.search_media(
                query,
                year=str(filters.get("year") or ""),
            )
            items = [
                self._discovery_item_from_candidate(candidate)
                for group in groups
                for candidate in list(getattr(group, "items", []) or [])
            ]
            return DiscoveryResult(items=items, total=len(items), source_label="搜索")
        if normalized_tab == "trending":
            result = self._discovery_service.trending(
                DiscoveryQuery(
                    kind="trending",
                    media_type=str(filters.get("media_type") or "tv"),
                    list_key=str(filters.get("list_key") or "trending_week"),
                    page=page,
                )
            )
            self._store_discovery_memory_cache(cache_key, result)
            return self._clone_discovery_result(result)
        if normalized_tab == "discover":
            result = self._discovery_service.discover(
                DiscoveryQuery(
                    kind="discover",
                    media_type=str(filters.get("media_type") or "tv"),
                    sort_by=str(filters.get("sort_by") or ""),
                    year=str(filters.get("year") or ""),
                    with_genres=str(filters.get("with_genres") or ""),
                    with_origin_country=str(filters.get("with_origin_country") or ""),
                    page=page,
                )
            )
            self._store_discovery_memory_cache(cache_key, result)
            return self._clone_discovery_result(result)
        raise RuntimeError(f"unsupported discovery tab: {normalized_tab}")

    def _discovery_memory_cache_key(self, tab_key: str, *, page: int, filters: dict[str, str]) -> str:
        normalized_tab = str(tab_key or "").strip() or "recommendation"
        if normalized_tab == "search":
            return ""
        payload = {
            "tab": normalized_tab,
            "page": int(page or 1),
            "filters": {str(key): str(value) for key, value in sorted(filters.items())},
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def _store_discovery_memory_cache(self, cache_key: str, result: DiscoveryResult) -> None:
        if cache_key:
            self._discovery_memory_cache[cache_key] = self._clone_discovery_result(result)

    @staticmethod
    def _clone_discovery_result(result: DiscoveryResult) -> DiscoveryResult:
        return replace(result, items=list(result.items or []))

    def _discovery_item_from_candidate(self, candidate) -> DiscoveryItem:
        raw = dict(getattr(candidate, "raw", {}) or {})
        provider_id = str(getattr(candidate, "provider_id", "") or "").strip()
        tmdb_id = str(raw.get("tmdb_id") or "").strip()
        if not tmdb_id and provider_id.startswith(("tv:", "movie:")):
            tmdb_id = provider_id.split(":")[1]
        media_type = "tv" if provider_id.startswith("tv:") else ("movie" if provider_id.startswith("movie:") else "")
        return DiscoveryItem(
            provider=str(getattr(candidate, "provider", "") or "tmdb"),
            provider_id=provider_id,
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=str(getattr(candidate, "title", "") or "").strip(),
            year=str(getattr(candidate, "year", "") or "").strip(),
            poster=str(raw.get("poster") or raw.get("poster_url") or "").strip(),
            backdrop=str(raw.get("backdrop") or raw.get("backdrop_url") or "").strip(),
            rating=str(raw.get("rating") or "").strip(),
            overview=str(raw.get("overview") or "").strip(),
            original_language=str(raw.get("original_language") or "").strip(),
            original_name=str(raw.get("original_name") or "").strip(),
            source_label="搜索",
        )

    def _load_recommendation_result(self, *, page: int) -> DiscoveryResult:
        seeds = self._build_recommendation_seeds(limit=self._RECOMMENDATION_SEED_LIMIT)
        favorite_provider_ids = set()
        if self._favorite_tmdb_binding_repository is not None and hasattr(
            self._favorite_tmdb_binding_repository, "load_recent"
        ):
            favorite_provider_ids = {
                str(binding.provider_id or "").strip()
                for binding in self._favorite_tmdb_binding_repository.load_recent(limit=200)
                if str(getattr(binding, "provider_id", "") or "").strip()
            }
        following_provider_ids = {
            str(record.provider_id or "").strip()
            for record in self._repository.load_recent_recommendation_candidates(limit=200)
            if str(record.provider_id or "").strip()
        }
        result = self._discovery_service.recommend(
            seeds=seeds,
            favorite_provider_ids=favorite_provider_ids,
            following_provider_ids=following_provider_ids,
        )
        if result.items:
            return result
        fallback = self._discovery_service.trending(
            DiscoveryQuery(kind="trending", media_type="tv", list_key="trending_week", page=page)
        )
        fallback.fallback_reason = "recommendation-empty"
        return fallback

    def load_related_recommendations(self, following_id: int) -> DiscoveryResult:
        if self._discovery_service is None or not hasattr(self._discovery_service, "related"):
            return DiscoveryResult(items=[], total=0, source_label="关联推荐")
        record = self._repository.get(following_id)
        if record is None:
            raise KeyError(f"following not found: {following_id}")
        snapshot = self._repository.get_detail_snapshot(following_id) or FollowingDetailSnapshot(
            following_id=following_id
        )
        identity = self._related_tmdb_identity(record, snapshot)
        douban_id = self._related_douban_id(record, snapshot)
        prefer_douban = bool(douban_id and self._should_prefer_douban_related(record, snapshot))
        if identity is None and not prefer_douban:
            return DiscoveryResult(items=[], total=0, source_label="关联推荐")
        media_type, tmdb_id = identity or (self._related_media_type_for_record(record, snapshot), "")
        excluded_provider_ids = self._related_excluded_provider_ids(record, snapshot)
        kwargs: dict[str, object] = {
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "excluded_provider_ids": excluded_provider_ids,
        }
        if prefer_douban:
            kwargs["douban_id"] = douban_id
            kwargs["prefer_douban"] = True
        return self._discovery_service.related(**kwargs)

    def _related_tmdb_identity(
        self,
        record: FollowingRecord,
        snapshot: FollowingDetailSnapshot,
    ) -> tuple[str, str] | None:
        provider_id_identity = self._tmdb_identity_from_provider_id(record.provider_id)
        if str(record.provider or "").strip() == "tmdb" and provider_id_identity is not None:
            return provider_id_identity
        tmdb_id = str((record.external_ids or {}).get("tmdb") or "").strip()
        if tmdb_id:
            media_type = self._related_media_type_for_record(record, snapshot)
            if media_type:
                return media_type, tmdb_id
        if provider_id_identity is not None:
            return provider_id_identity
        bundle = snapshot.metadata_bundle
        if bundle is not None:
            for source in (bundle.source_snapshots or {}).values():
                if str(source.provider or "").strip() != "tmdb":
                    continue
                identity = self._tmdb_identity_from_provider_id(source.provider_id)
                if identity is not None:
                    return identity
        return None

    @staticmethod
    def _tmdb_identity_from_provider_id(provider_id: object) -> tuple[str, str] | None:
        parts = str(provider_id or "").strip().split(":")
        if len(parts) < 2 or parts[0] not in {"tv", "movie"} or not parts[1]:
            return None
        return parts[0], parts[1]

    def _related_media_type_for_record(
        self,
        record: FollowingRecord,
        snapshot: FollowingDetailSnapshot,
    ) -> str:
        provider_id_identity = self._tmdb_identity_from_provider_id(record.provider_id)
        if provider_id_identity is not None:
            return provider_id_identity[0]
        return "movie" if _resolve_effective_media_kind(record, snapshot) == "movie" else "tv"

    def _related_excluded_provider_ids(
        self,
        record: FollowingRecord,
        snapshot: FollowingDetailSnapshot,
    ) -> set[str]:
        excluded: set[str] = set()
        for candidate in [record.provider_id]:
            identity = self._tmdb_identity_from_provider_id(candidate)
            if identity is not None:
                excluded.add(f"{identity[0]}:{identity[1]}")
        tmdb_id = str((record.external_ids or {}).get("tmdb") or "").strip()
        if tmdb_id:
            media_type = self._related_media_type_for_record(record, snapshot)
            if media_type:
                excluded.add(f"{media_type}:{tmdb_id}")
        bundle = snapshot.metadata_bundle
        if bundle is not None:
            for source in (bundle.source_snapshots or {}).values():
                identity = self._tmdb_identity_from_provider_id(source.provider_id)
                if identity is not None:
                    excluded.add(f"{identity[0]}:{identity[1]}")
        return excluded

    def _related_douban_id(
        self,
        record: FollowingRecord,
        snapshot: FollowingDetailSnapshot,
    ) -> str:
        external_id = str((record.external_ids or {}).get("douban") or "").strip()
        if external_id:
            return external_id
        if str(record.provider or "").strip() in {"official_douban", "local_douban", "douban"}:
            provider_id = str(record.provider_id or "").strip()
            if provider_id.isdigit():
                return provider_id
        bundle = snapshot.metadata_bundle
        if bundle is not None:
            for source in (bundle.source_snapshots or {}).values():
                if str(source.provider or "").strip() not in {"official_douban", "local_douban", "douban"}:
                    continue
                provider_id = str(source.provider_id or "").strip()
                if provider_id.isdigit():
                    return provider_id
        for fields in self._related_metadata_field_sets(snapshot):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                if str(field.get("label") or "").strip() != "豆瓣ID":
                    continue
                value = str(field.get("value") or "").strip()
                if value.isdigit():
                    return value
        return ""

    def _should_prefer_douban_related(
        self,
        record: FollowingRecord,
        snapshot: FollowingDetailSnapshot,
    ) -> bool:
        media_kind = _resolve_effective_media_kind(record, snapshot)
        if media_kind not in {"movie", "live_action", "anime", "variety", "documentary"}:
            return False
        return self._is_domestic_following(record, snapshot)

    def _is_domestic_following(
        self,
        record: FollowingRecord,
        snapshot: FollowingDetailSnapshot,
    ) -> bool:
        texts = [record.title, record.original_title, record.media_kind]
        for fields in self._related_metadata_field_sets(snapshot):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                label = str(field.get("label") or "").strip()
                if label in {"地区", "国家", "制片国家/地区", "类型"}:
                    texts.append(str(field.get("value") or ""))
        text = " ".join(texts).lower()
        return any(
            marker in text
            for marker in (
                "中国大陆",
                "中国内地",
                "内地",
                "大陆",
                "中国",
                "国产",
                "国创",
                "cn",
            )
        )

    @staticmethod
    def _related_metadata_field_sets(snapshot: FollowingDetailSnapshot) -> list[list[dict[str, object]]]:
        field_sets: list[list[dict[str, object]]] = []
        field_sets.append(list(getattr(snapshot, "metadata_fields", []) or []))
        bundle = snapshot.metadata_bundle
        if bundle is not None:
            merged = getattr(bundle, "merged_snapshot", None)
            if merged is not None:
                field_sets.append(list(getattr(merged, "metadata_fields", []) or []))
            for source in (bundle.source_snapshots or {}).values():
                field_sets.append(list(getattr(source, "metadata_fields", []) or []))
        return field_sets

    def _build_recommendation_seeds(self, *, limit: int) -> list[RecommendationSeed]:
        seeds: list[RecommendationSeed] = []
        seen_provider_ids: set[str] = set()
        for record in self._repository.load_recent_recommendation_candidates(limit=limit):
            provider_id = str(record.provider_id or "").strip()
            tmdb_id = str((record.external_ids or {}).get("tmdb") or "").strip()
            if provider_id.startswith("tv:") and not tmdb_id:
                tmdb_id = provider_id.split(":")[1]
            elif provider_id.startswith("movie:") and not tmdb_id:
                tmdb_id = provider_id.split(":")[1]
            if not provider_id or not tmdb_id or provider_id in seen_provider_ids:
                continue
            seen_provider_ids.add(provider_id)
            media_type = "tv" if provider_id.startswith("tv:") else "movie"
            activity_weight = 5.0 if bool(record.has_update) else 3.0
            if int(record.last_played_at or 0) > 0:
                activity_weight += 1.0
            seeds.append(
                RecommendationSeed(
                    provider_id=provider_id,
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    seed_source="following",
                    activity_weight=activity_weight,
                    activity_timestamp=max(int(record.last_played_at or 0), int(record.updated_at or 0)),
                    reason_flags=["has_update"] if bool(record.has_update) else [],
                )
            )
        if self._favorite_tmdb_binding_repository is not None and hasattr(
            self._favorite_tmdb_binding_repository, "load_recent"
        ):
            remaining = max(0, int(limit or 0) - len(seeds))
            for binding in self._favorite_tmdb_binding_repository.load_recent(limit=max(remaining, limit)):
                provider_id = str(getattr(binding, "provider_id", "") or "").strip()
                tmdb_id = str(getattr(binding, "tmdb_id", "") or "").strip()
                media_type = str(getattr(binding, "media_type", "") or "").strip() or (
                    "tv" if provider_id.startswith("tv:") else "movie"
                )
                if not provider_id or not tmdb_id or provider_id in seen_provider_ids:
                    continue
                seen_provider_ids.add(provider_id)
                seeds.append(
                    RecommendationSeed(
                        provider_id=provider_id,
                        tmdb_id=tmdb_id,
                        media_type=media_type,
                        seed_source="favorite",
                        activity_weight=2.0,
                        activity_timestamp=int(getattr(binding, "updated_at", 0) or 0),
                        reason_flags=[],
                    )
                )
                if len(seeds) >= max(0, int(limit or 0)):
                    break
        return seeds

    def _search_tmdb_following(self, query: MetadataQuery):
        search_fn = getattr(self._metadata_search_service, "search_following", None)
        if callable(search_fn):
            return search_fn(query, provider_filter="tmdb")
        return self._metadata_search_service.search(query, provider_filter="tmdb")

    def _sort_following_group_items(self, group):
        if str(getattr(group, "provider", "") or "").strip() != "tmdb":
            return group
        items = list(getattr(group, "items", []) or [])
        items.sort(key=self._following_candidate_sort_key)
        return replace(group, items=items)

    def _following_candidate_sort_key(self, candidate) -> int:
        provider_id = str(getattr(candidate, "provider_id", "") or "").strip()
        if provider_id.startswith("tv:"):
            return 0
        if provider_id.startswith("movie:"):
            return 1
        return 2

    def candidate_from_url(self, url: str):
        provider_options = getattr(self._metadata_search_service, "provider_options", None)
        available = set()
        if callable(provider_options):
            try:
                available = {str(provider) for provider, _label in provider_options()}
            except Exception:
                available = set()
        return following_candidate_from_url(url, available_providers=available)

    def _hydrate_url_candidate(self, candidate):
        detail_record, _detail_error = load_candidate_detail_record(self._metadata_search_service, candidate)
        if detail_record is None:
            return candidate
        raw = dict(getattr(candidate, "raw", {}) or {})
        detail_fields = list(getattr(detail_record, "detail_fields", []) or [])
        if detail_fields:
            raw["detail_fields"] = detail_fields
        tmdb_id = str(getattr(detail_record, "tmdb_id", "") or "").strip()
        if tmdb_id:
            raw["tmdb_id"] = tmdb_id
        overview = str(getattr(detail_record, "overview", "") or "").strip()
        if overview:
            raw["overview"] = overview
        rating = str(getattr(detail_record, "rating", "") or "").strip()
        if rating:
            raw["rating"] = rating
        poster = str(getattr(detail_record, "poster", "") or "").strip()
        if poster:
            raw["poster"] = poster
        backdrop = str(getattr(detail_record, "backdrop", "") or "").strip()
        if backdrop:
            raw["backdrop"] = backdrop
        return replace(
            candidate,
            title=str(getattr(detail_record, "title", "") or getattr(candidate, "title", "") or "").strip(),
            year=str(getattr(detail_record, "year", "") or getattr(candidate, "year", "") or "").strip(),
            raw=raw,
        )

    def _candidate_for_add(self, candidate):
        if isinstance(candidate, DiscoveryItem):
            provider = str(candidate.provider or "").strip() or "tmdb"
            raw = {
                "tmdb_id": str(candidate.tmdb_id or "").strip(),
                "poster_url": str(candidate.poster or "").strip(),
                "backdrop_url": str(candidate.backdrop or "").strip(),
                "rating": str(candidate.rating or "").strip(),
                "overview": str(candidate.overview or "").strip(),
            }
            normalized_raw = {key: value for key, value in raw.items() if value}
            return MetadataScrapeCandidate(
                provider=provider,
                provider_label={
                    "tmdb": "TMDB",
                    "bangumi": "Bangumi",
                    "douban": "豆瓣",
                    "bilibili": "B站",
                    "iqiyi": "爱奇艺",
                    "tencent": "腾讯",
                    "youku": "优酷",
                    "mgtv": "芒果",
                    "sohu": "搜狐",
                }.get(provider, provider),
                provider_id=str(candidate.provider_id or "").strip(),
                title=str(candidate.title or "").strip(),
                year=str(candidate.year or "").strip(),
                subtitle="电影" if str(candidate.media_type or "").strip() == "movie" else "剧集",
                raw=normalized_raw,
            )
        return candidate

    def add_candidate(self, candidate, *, current_episode: int = 0) -> FollowingRecord:
        now = self._now()
        candidate = self._candidate_for_add(candidate)
        record, snapshot = build_following_from_metadata_candidate(
            candidate,
            metadata_search_service=self._metadata_search_service,
            now=now,
        )
        existing = self._repository.get_by_identity(record.provider, record.provider_id)
        if existing is not None:
            record = self._merge_existing_candidate_state(
                record,
                existing,
                current_episode=current_episode,
            )
        if current_episode > 0:
            current_season_number = resolve_progress_season(
                record.current_season_number or record.season_number,
                current_episode,
                fallback_season=record.season_number,
            )
            watched_latest = progress_at_or_beyond(
                current_season_number,
                current_episode,
                record.season_number,
                record.latest_episode,
                current_fallback_season=record.season_number,
                latest_fallback_season=record.season_number,
            )
            record.current_season_number = current_season_number
            record.current_episode = current_episode
            record.watched_latest_episode = watched_latest
            if watched_latest:
                record.has_update = False
                record.new_episode_count = 0
                record.homepage_prompt_pending = False
        record_id = self._repository.upsert(record)
        saved = self._repository.get(record_id)
        if saved is None:
            raise RuntimeError("追更保存失败")
        snapshot.following_id = record_id
        self._repository.save_detail_snapshot(record_id, snapshot)
        self._discovery_memory_cache.clear()
        return saved

    def _merge_existing_candidate_state(
        self,
        record: FollowingRecord,
        existing: FollowingRecord,
        *,
        current_episode: int,
    ) -> FollowingRecord:
        target_episode = current_episode if current_episode > 0 else existing.current_episode
        target_season_number = (
            resolve_progress_season(
                record.season_number,
                current_episode,
                fallback_season=record.season_number,
            )
            if current_episode > 0
            else existing.current_season_number
        )
        keep_position = existing.current_episode > 0 and target_episode == existing.current_episode
        watched_latest = progress_at_or_beyond(
            target_season_number,
            target_episode,
            record.season_number,
            record.latest_episode,
            current_fallback_season=record.season_number,
            latest_fallback_season=record.season_number,
        )
        return replace(
            record,
            source_bindings=list(existing.source_bindings or record.source_bindings),
            current_season_number=target_season_number,
            current_episode=target_episode,
            position_seconds=existing.position_seconds if keep_position else 0,
            watched_latest_episode=watched_latest,
            has_update=False if watched_latest else existing.has_update,
            new_episode_count=0 if watched_latest else existing.new_episode_count,
            homepage_prompt_pending=False if watched_latest else existing.homepage_prompt_pending,
            prompt_snoozed_until=existing.prompt_snoozed_until,
            prompt_dismissed_latest_episode=existing.prompt_dismissed_latest_episode,
            prompt_dismissed_latest_season=existing.prompt_dismissed_latest_season,
            created_at=existing.created_at or record.created_at,
            last_played_at=existing.last_played_at if keep_position else 0,
            last_checked_at=existing.last_checked_at,
            next_check_after=existing.next_check_after or record.next_check_after,
        )

    def load_page(self, *, page: int, size: int, keyword: str, only_updates: bool):
        normalized_size = max(1, int(size or 1))
        records, total = self._repository.load_page(page=1, size=normalized_size, keyword=keyword, only_updates=only_updates)
        if total > len(records):
            records, total = self._repository.load_page(page=1, size=total, keyword=keyword, only_updates=only_updates)
        cards = []
        for record in records:
            snapshot = self._repository.get_detail_snapshot(record.id) or FollowingDetailSnapshot(following_id=record.id)
            completion_state = self._completion_state(record, snapshot)
            effective_kind = _resolve_effective_media_kind(record, snapshot)
            cards.append(
                FollowingCardItem(
                    record=record,
                    display_title=record.title,
                    subtitle=_media_kind_category(effective_kind),
                    progress_text=self._progress_text(record, snapshot=snapshot, completion_state=completion_state, media_kind=effective_kind),
                    update_text=self._update_text(record, completion_state=completion_state, snapshot=snapshot, media_kind=effective_kind),
                    updated_hint=record.has_update,
                    error_text=record.last_error,
                )
            )
        cards.sort(key=self._following_card_sort_key)
        offset = max(0, int(page or 1) - 1) * normalized_size
        return cards[offset : offset + normalized_size], total

    def _following_card_sort_key(self, card: FollowingCardItem) -> tuple[int, int, int, int, int]:
        record = card.record
        snapshot = self._repository.get_detail_snapshot(record.id) or FollowingDetailSnapshot(following_id=record.id)
        completion_state = self._completion_state(record, snapshot)
        if completion_state == FollowingCompletionState.COMPLETED:
            return (1, 1, 0, 0, -int(record.updated_at or 0))
        now = datetime.fromtimestamp(self._now())
        next_update_date = self._next_update_sort_date(snapshot, now=now)
        if next_update_date is None:
            return (0, 1, 0, 0, -int(record.updated_at or 0))
        return (0, 0, next_update_date[0], next_update_date[1], -int(record.updated_at or 0))

    @staticmethod
    def _next_update_sort_date(snapshot: FollowingDetailSnapshot, *, now: datetime) -> tuple[int, int] | None:
        dates: list[tuple[int, int]] = []
        for platform in FollowingController._snapshot_playback_platforms(snapshot):
            parsed = FollowingController._parse_update_time_text(
                " ".join(
                    text
                    for text in (platform.update_time_text, platform.status_text)
                    if str(text or "").strip()
                ),
                now=now,
            )
            if parsed is not None:
                dates.append(parsed)
        if snapshot.next_episode is not None:
            parsed = FollowingController._parse_air_date(snapshot.next_episode.air_date)
            if parsed is not None and parsed[0] >= now.date().toordinal():
                dates.append(parsed)
        for episode in snapshot.episodes:
            parsed = FollowingController._parse_air_date(episode.air_date)
            if parsed is not None and parsed[0] >= now.date().toordinal():
                dates.append(parsed)
        return min(dates) if dates else None

    @staticmethod
    def _snapshot_playback_platforms(snapshot: FollowingDetailSnapshot):
        bundle = snapshot.metadata_bundle
        if bundle is None:
            return []
        platforms = list(bundle.merged_snapshot.playback_platforms or [])
        for source in (bundle.source_snapshots or {}).values():
            platforms.extend(source.playback_platforms or [])
        return platforms

    @staticmethod
    def _parse_update_time_text(value: str, *, now: datetime) -> tuple[int, int] | None:
        text = str(value or "").strip()
        if not text:
            return None
        time_match = re.search(r"(\d{1,2})[:：](\d{2})", text)
        minutes = 24 * 60
        if time_match is not None:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                minutes = hour * 60 + minute
        weekdays = FollowingController._parse_update_weekdays(text)
        if weekdays:
            candidates: list[tuple[int, int]] = []
            for weekday in weekdays:
                days = (weekday - now.weekday()) % 7
                candidate_date = now.date() + timedelta(days=days)
                if days == 0 and minutes != 24 * 60 and minutes < now.hour * 60 + now.minute:
                    candidate_date += timedelta(days=7)
                candidates.append((candidate_date.toordinal(), minutes))
            return min(candidates)
        date_match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
        if date_match is None:
            return None
        try:
            parsed = datetime(
                int(date_match.group(1)),
                int(date_match.group(2)),
                int(date_match.group(3)),
            ).date()
        except ValueError:
            return None
        if parsed < now.date():
            return None
        return parsed.toordinal(), minutes

    @staticmethod
    def _parse_update_weekdays(text: str) -> list[int]:
        weekday_map = {
            "一": 0,
            "二": 1,
            "三": 2,
            "四": 3,
            "五": 4,
            "六": 5,
            "日": 6,
            "天": 6,
            "1": 0,
            "2": 1,
            "3": 2,
            "4": 3,
            "5": 4,
            "6": 5,
            "7": 6,
        }
        weekdays: list[int] = []
        seen: set[int] = set()
        for match in re.finditer(r"(?:每|周|星期|礼拜)([一二三四五六日天1-7、,，/和至到]+)", text):
            segment = match.group(1)
            if any(separator in segment for separator in ("至", "到")):
                parts = re.split(r"[至到]", segment, maxsplit=1)
                if len(parts) == 2:
                    start = weekday_map.get(parts[0][-1:])
                    end = weekday_map.get(parts[1][:1])
                    if start is not None and end is not None:
                        indexes = list(range(start, end + 1)) if start <= end else [*range(start, 7), *range(0, end + 1)]
                        for index in indexes:
                            if index not in seen:
                                seen.add(index)
                                weekdays.append(index)
                        continue
            for char in segment:
                index = weekday_map.get(char)
                if index is None or index in seen:
                    continue
                seen.add(index)
                weekdays.append(index)
        return weekdays

    @staticmethod
    def _parse_air_date(value: str) -> tuple[int, int] | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
        return parsed.toordinal(), 24 * 60

    def _update_text(self, record: FollowingRecord, *, completion_state: str | None = None, snapshot: FollowingDetailSnapshot | None = None, media_kind: str = "") -> str:
        if media_kind == "movie":
            if snapshot is not None and snapshot.next_episode is not None:
                return "待播出"
            return ""
        if record.has_update:
            snapshot = snapshot or FollowingDetailSnapshot(following_id=record.id)
            current_season_number = resolve_progress_season(
                record.current_season_number,
                record.current_episode,
                fallback_season=record.season_number,
            )
            latest_season_number = self._latest_season_number(record, snapshot)
            count = resolve_new_episode_count(
                has_update=True,
                current_season_number=current_season_number,
                current_episode=record.current_episode,
                latest_season_number=latest_season_number,
                latest_episode=record.latest_episode,
                fallback_count=record.new_episode_count,
                total_episodes=record.total_episodes,
                seasons=snapshot.seasons,
                episodes=snapshot.episodes,
            )
            return f"有 {count} 集更新"
        resolved = completion_state or self._completion_state(record)
        return "已完结" if resolved == FollowingCompletionState.COMPLETED else "连载中"

    def _completion_state(self, record: FollowingRecord, snapshot: FollowingDetailSnapshot | None = None) -> str:
        snapshot = snapshot or self._repository.get_detail_snapshot(record.id) or FollowingDetailSnapshot(following_id=record.id)
        return resolve_following_completion_state(
            episodes=snapshot.episodes,
            next_episode=snapshot.next_episode,
            today=datetime.fromtimestamp(self._now()).date(),
        )

    def search_items(self, keyword: str, page: int) -> tuple[list[FollowingCardItem], int]:
        return self.load_page(page=page, size=20, keyword=keyword, only_updates=False)

    def load_detail(
        self,
        following_id: int,
        *,
        refresh_if_empty: bool = True,
        include_ai_summary: bool = True,
    ) -> FollowingDetailView:
        record = self._repository.get(following_id)
        if record is None:
            raise KeyError(f"following not found: {following_id}")
        snapshot = self._repository.get_detail_snapshot(following_id) or FollowingDetailSnapshot(
            following_id=following_id
        )
        if refresh_if_empty and self._snapshot_needs_refresh(snapshot) and self._update_service is not None:
            self._update_service.check_record(following_id)
            record = self._repository.get(following_id) or record
            snapshot = self._repository.get_detail_snapshot(following_id) or snapshot
        snapshot_for_ai = snapshot
        original_bundle = snapshot.metadata_bundle
        snapshot = self._ensure_metadata_bundle(record, snapshot)
        if original_bundle is None and snapshot.metadata_bundle is not None:
            snapshot.following_id = snapshot.following_id or following_id
            save_snapshot = getattr(self._repository, "save_detail_snapshot", None)
            if callable(save_snapshot):
                save_snapshot(following_id, snapshot)
        if include_ai_summary and snapshot_for_ai.metadata_bundle is None:
            ai_snapshot = self._with_ai_summary(record, snapshot_for_ai)
            if ai_snapshot.ai_summary is not None:
                snapshot = replace(snapshot, ai_summary=ai_snapshot.ai_summary)
        return FollowingDetailView(record=record, snapshot=snapshot)

    def _with_ai_summary(
        self,
        record: FollowingRecord,
        snapshot: FollowingDetailSnapshot,
    ) -> FollowingDetailSnapshot:
        if self._ai_enrichment_service is None:
            return snapshot
        summarize = getattr(self._ai_enrichment_service, "summarize_following_detail", None)
        if not callable(summarize):
            return snapshot
        next_episode = snapshot.next_episode
        try:
            result = summarize(
                FollowingDetailSummaryInput(
                    title=record.title,
                    media_kind=record.media_kind,
                    current_episode=record.current_episode,
                    latest_episode=record.latest_episode,
                    total_episodes=record.total_episodes,
                    overview=snapshot.overview,
                    next_episode_title="" if next_episode is None else next_episode.title,
                    next_episode_air_date="" if next_episode is None else next_episode.air_date,
                    metadata_fields=[
                        {
                            "label": str(item.get("label", "")),
                            "value": str(item.get("value", "")),
                        }
                        for item in snapshot.metadata_fields[:12]
                    ],
                )
            )
        except Exception:
            return snapshot
        summary = str(getattr(result, "summary", "") or "").strip()
        highlights = [
            str(item or "").strip()
            for item in getattr(result, "highlights", []) or []
            if str(item or "").strip()
        ][:3]
        next_hint = str(getattr(result, "next_hint", "") or "").strip()
        if not summary and not highlights and not next_hint:
            return snapshot
        return replace(
            snapshot,
            ai_summary=FollowingAISummary(
                summary=summary,
                highlights=highlights,
                next_hint=next_hint,
            ),
        )

    def load_detail_season(self, following_id: int, *, season_number: int) -> FollowingDetailView:
        record = self._repository.get(following_id)
        if record is None:
            raise KeyError(f"following not found: {following_id}")
        snapshot = self._repository.get_detail_snapshot(following_id) or FollowingDetailSnapshot(
            following_id=following_id
        )
        candidate = self._tmdb_detail_candidate_for_season(record, season_number=season_number)
        if candidate is None:
            return FollowingDetailView(record=record, snapshot=snapshot)
        detail_record, detail_error = load_candidate_detail_record_full(
            self._metadata_search_service,
            candidate,
        )
        if detail_record is None:
            raise RuntimeError(detail_error or "没有找到该季元数据")
        _detail_following, detail_snapshot = build_snapshot_from_record(
            detail_record,
            now=self._now(),
            media_kind=record.media_kind,
        )
        merged_snapshot = merge_following_snapshot(
            snapshot,
            detail_snapshot,
            fill_missing=True,
            prefer_episodes=True,
        )
        merged_snapshot.following_id = following_id
        self._repository.save_detail_snapshot(following_id, merged_snapshot)
        return FollowingDetailView(record=record, snapshot=merged_snapshot)

    def add_from_player(
        self,
        *,
        vod: VodItem,
        item: PlayItem,
        source_kind: str,
        source_key: str,
        position_seconds: int,
        playlist: list[PlayItem] | None = None,
        mark_current_episode: bool = True,
    ) -> FollowingRecord:
        now = self._now()
        playlist_items = list(playlist or [item])
        playlist_numbers = self._playlist_episode_numbers(playlist_items)
        inferred_episode_number = self._playlist_position(item, playlist_items) or infer_playlist_episode_number(item, playlist_items) or 0
        episode_number = inferred_episode_number if mark_current_episode else 0
        provider_id = f"{source_kind}:{source_key}:{vod.vod_id or item.vod_id or item.media_title or item.title}"
        external_ids = self._external_ids_from_vod(vod, item)
        metadata_raw_episodes = self._metadata_raw_episodes_from_vod(vod)
        metadata_latest, metadata_total = compute_episode_counts(metadata_raw_episodes, now=now)
        playlist_latest = max(playlist_numbers) if playlist_numbers else 0
        latest_episode = max(playlist_latest, min(metadata_latest, playlist_latest) if playlist_latest else metadata_latest)
        total_episodes = max(metadata_total, self._metadata_total_episodes_from_vod(vod), len(playlist_numbers))
        media_kind = str(vod.category_name or vod.type_name or item.category_name or item.type_name or "").strip()
        season_number = (
            extract_season_number(vod.vod_name)
            or extract_season_number(item.media_title)
            or extract_season_number(item.original_title)
            or 0
        )
        record = FollowingRecord(
            id=0,
            title=str(vod.vod_name or item.media_title or item.title or "").strip(),
            original_title=str(item.original_title or "").strip(),
            media_kind=media_kind,
            season_number=season_number,
            poster=str(vod.vod_pic or item.video_cover_override or "").strip(),
            rating=str(vod.vod_remarks or "").strip(),
            provider="player",
            provider_id=provider_id,
            provider_priority=provider_priority_for_media_kind("anime" if "动漫" in media_kind or "动画" in media_kind else "live_action"),
            external_ids=external_ids,
            source_bindings=[
                FollowingSourceBinding(
                    source_kind=source_kind,
                    source_key=source_key,
                    vod_id=str(vod.vod_id or item.vod_id or "").strip(),
                    provider="player",
                    provider_id=provider_id,
                )
            ],
            current_season_number=resolve_progress_season(season_number, episode_number, fallback_season=season_number),
            current_episode=episode_number,
            position_seconds=position_seconds if mark_current_episode else 0,
            latest_episode=latest_episode,
            previous_latest_episode=latest_episode,
            total_episodes=total_episodes,
            watched_latest_episode=progress_at_or_beyond(
                season_number,
                episode_number,
                season_number,
                latest_episode,
                current_fallback_season=season_number,
                latest_fallback_season=season_number,
            ),
            created_at=now,
            updated_at=now,
            last_played_at=now if mark_current_episode else 0,
            next_check_after=now,
        )
        if record.watched_latest_episode:
            record.has_update = False
            record.new_episode_count = 0
            record.homepage_prompt_pending = False
        record_id = self._repository.upsert(record)
        saved = self._repository.get(record_id)
        if saved is None:
            raise RuntimeError("追更保存失败")
        snapshot = self._snapshot_from_vod(vod, playlist_items, metadata_raw_episodes=metadata_raw_episodes, refreshed_at=now)
        if self._snapshot_has_data(snapshot):
            snapshot.following_id = record_id
            self._repository.save_detail_snapshot(record_id, snapshot)
        return saved

    def mark_watched_latest(self, following_id: int) -> None:
        record = self._repository.get(following_id)
        if record is None:
            return
        self._repository.update_progress(
            following_id,
            current_season_number=resolve_progress_season(
                record.season_number,
                record.latest_episode,
                fallback_season=record.season_number,
            ),
            current_episode=record.latest_episode,
            position_seconds=0,
            last_played_at=self._now(),
        )
        self._repository.clear_homepage_prompt(following_id)

    def record_playback_progress(
        self,
        following_id: int,
        *,
        current_season_number: int = 0,
        current_episode: int,
        position_seconds: int,
        allow_regression: bool = False,
    ) -> None:
        record = self._repository.get(following_id)
        if current_season_number <= 0:
            current_season_number = (
                resolve_progress_season(
                    getattr(record, "current_season_number", 0) if record is not None else 0,
                    current_episode,
                    fallback_season=getattr(record, "season_number", 0) if record is not None else 0,
                )
            )
        if not allow_regression and record is not None and compare_progress(
            current_season_number,
            current_episode,
            record.current_season_number,
            record.current_episode,
            current_fallback_season=record.season_number,
            target_fallback_season=record.season_number,
        ) < 0:
            return
        self._repository.update_progress(
            following_id,
            current_season_number=current_season_number,
            current_episode=current_episode,
            position_seconds=position_seconds,
            last_played_at=self._now(),
        )

    def record_playback_source(
        self,
        following_id: int,
        *,
        source_kind: str,
        source_key: str = "",
        vod_id: str,
        current_season_number: int,
        current_episode: int,
        playlist_latest_episode: int = 0,
    ) -> None:
        record = self._repository.get(following_id)
        if record is None:
            return
        normalized_kind = str(source_kind or "").strip()
        normalized_key = str(source_key or "").strip()
        normalized_vod_id = str(vod_id or "").strip()
        if not normalized_kind or not normalized_vod_id:
            return
        can_update_binding = compare_progress(
            current_season_number,
            current_episode,
            record.current_season_number,
            record.current_episode,
            current_fallback_season=record.season_number,
            target_fallback_season=record.season_number,
        ) >= 0
        bindings = list(record.source_bindings or [])
        if can_update_binding:
            new_binding = FollowingSourceBinding(
                source_kind=normalized_kind,
                source_key=normalized_key,
                vod_id=normalized_vod_id,
            )
            bindings = [
                binding
                for binding in bindings
                if not (
                    binding.source_kind == new_binding.source_kind
                    and binding.source_key == new_binding.source_key
                    and binding.vod_id == new_binding.vod_id
                )
            ]
            bindings.insert(0, new_binding)
        self._repository.update_playback_source_state(
            following_id,
            source_bindings=bindings,
            latest_episode=max(record.latest_episode, int(playlist_latest_episode or 0)),
            updated_at=self._now(),
        )

    def clear_homepage_prompt(self, following_id: int) -> None:
        self._repository.clear_homepage_prompt(following_id)

    def snooze_prompt(self, following_id: int) -> None:
        self._repository.snooze_prompt(following_id, until=self._now() + 24 * 3600)

    def dismiss_prompt_until_next_episode(self, following_id: int) -> None:
        self._repository.dismiss_prompt_until_next_episode(following_id)

    def load_homepage_prompts(self) -> list[FollowingRecord]:
        return self._repository.load_homepage_prompt_records(now=self._now())

    def check_one(self, following_id: int):
        if self._update_service is None:
            return None
        return self._update_service.check_record(following_id)

    def refresh_metadata(self, following_id: int) -> FollowingDetailView:
        record = self._repository.get(following_id)
        if record is None:
            raise KeyError(f"following not found: {following_id}")
        existing_snapshot = self._repository.get_detail_snapshot(following_id)
        candidate = self._tmdb_refresh_candidate_from_record(record)
        if self._should_skip_tmdb_refresh_without_known_season(record):
            candidate = None
        include_related = False
        if candidate is None:
            candidate = (
                self._metadata_refresh_candidate(record)
                or self._bangumi_refresh_candidate_from_record(record)
            )
        if candidate is None:
            raise RuntimeError("没有找到可用于更新元数据的匹配结果")
        detail_records: list[MetadataRecord] = []
        refreshed_record, snapshot = build_following_from_metadata_candidate(
            candidate,
            metadata_search_service=self._metadata_search_service,
            now=self._now(),
            media_kind=record.media_kind,
            include_related=include_related,
            use_full_detail=True,
            detail_record_sink=detail_records,
        )
        refreshed_record.current_episode = record.current_episode
        refreshed_record.current_season_number = record.current_season_number
        refreshed_record.position_seconds = record.position_seconds
        new_latest = max(refreshed_record.latest_episode, record.latest_episode)
        new_total = max(refreshed_record.total_episodes, record.total_episodes)
        refreshed_completion_state = resolve_following_completion_state(
            episodes=snapshot.episodes,
            next_episode=snapshot.next_episode,
        )
        if (
            refreshed_completion_state != FollowingCompletionState.COMPLETED
            and new_total > 0
            and new_latest > 0
            and new_total <= new_latest
        ):
            new_total = 0
        refreshed_record.latest_episode = new_latest
        refreshed_record.previous_latest_episode = record.previous_latest_episode
        refreshed_record.total_episodes = new_total
        latest_season_number = resolve_latest_released_season_number(
            record_season_number=refreshed_record.season_number,
            latest_episode=new_latest,
            current_season_number=record.current_season_number,
            episodes=snapshot.episodes,
            today=datetime.fromtimestamp(self._now()).date(),
        )
        has_update = not progress_at_or_beyond(
            record.current_season_number,
            record.current_episode,
            latest_season_number,
            new_latest,
            current_fallback_season=record.season_number,
            latest_fallback_season=latest_season_number,
        )
        new_episode_count = resolve_new_episode_count(
            has_update=has_update,
            current_episode=record.current_episode,
            latest_episode=new_latest,
        )
        homepage_prompt_pending = record.homepage_prompt_pending and has_update
        refreshed_record.has_update = has_update
        refreshed_record.new_episode_count = new_episode_count
        refreshed_record.homepage_prompt_pending = homepage_prompt_pending
        refreshed_record.watched_latest_episode = progress_at_or_beyond(
            record.current_season_number,
            record.current_episode,
            latest_season_number,
            new_latest,
            current_fallback_season=record.season_number,
            latest_fallback_season=latest_season_number,
        )
        refreshes_tmdb_candidate = str(getattr(candidate, "provider", "") or "").strip() == "tmdb"
        should_force_bundle_refresh = existing_snapshot is not None and existing_snapshot.metadata_bundle is not None
        if (
            refreshes_tmdb_candidate
            or should_force_bundle_refresh
            or existing_snapshot is None
            or self._snapshot_needs_refresh(existing_snapshot)
        ):
            snapshot = self._ensure_metadata_bundle(
                refreshed_record,
                snapshot,
                force=should_force_bundle_refresh,
                tmdb_detail_record=detail_records[0] if refreshes_tmdb_candidate and detail_records else None,
            )
        self._repository.update_metadata(following_id, refreshed_record)
        self._repository.update_check_state(
            following_id,
            latest_episode=new_latest,
            latest_season_number=latest_season_number,
            total_episodes=new_total,
            checked_at=record.last_checked_at,
            next_check_after=record.next_check_after,
            has_update=has_update,
            new_episode_count=new_episode_count,
            homepage_prompt_pending=homepage_prompt_pending,
            last_error=record.last_error,
        )
        if existing_snapshot is not None:
            snapshot = self._merge_refreshed_snapshot(existing_snapshot, snapshot)
        snapshot.following_id = following_id
        self._repository.save_detail_snapshot(following_id, snapshot)
        return FollowingDetailView(
            record=self._repository.get(following_id) or replace(refreshed_record, id=following_id),
            snapshot=snapshot,
        )

    def check_all_due(self):
        if self._update_service is None:
            results = []
        else:
            results = self._update_service.check_due_records()
        # 手动"检查更新"同时并入服务端追剧信号(元数据之后跑,latest 取 max 不被压回)。
        if self._backend_sync_service is not None:
            self._backend_sync_service.sync_blocking()
        return results

    def delete(self, following_id: int) -> None:
        self._repository.delete(following_id)

    def _metadata_refresh_candidate(self, record: FollowingRecord):
        query = MetadataQuery(
            title=record.title,
            category_name="动漫" if record.media_kind == "anime" else record.media_kind,
        )
        groups = self._metadata_search_service.search(query, provider_filter="tmdb")
        candidates = [item for group in groups for item in list(getattr(group, "items", []) or [])]
        if not candidates:
            return None
        tmdb_external_id = str(record.external_ids.get("tmdb") or "").strip()
        if tmdb_external_id:
            for candidate in candidates:
                if str(getattr(candidate, "provider", "") or "") != "tmdb":
                    continue
                if tmdb_external_id in str(getattr(candidate, "provider_id", "") or ""):
                    return candidate
        for candidate in candidates:
            if str(getattr(candidate, "provider", "") or "") == "tmdb":
                return candidate
        identity = (record.provider, record.provider_id)
        for candidate in candidates:
            if (
                str(getattr(candidate, "provider", "") or ""),
                str(getattr(candidate, "provider_id", "") or ""),
            ) == identity:
                return candidate
        for provider, external_id in record.external_ids.items():
            for candidate in candidates:
                if str(getattr(candidate, "provider", "") or "") != provider:
                    continue
                if str(external_id) in str(getattr(candidate, "provider_id", "") or ""):
                    return candidate
        provider_order = ["tmdb", record.provider, *list(record.provider_priority or []), "bangumi", "douban"]
        for provider in provider_order:
            for candidate in candidates:
                if str(getattr(candidate, "provider", "") or "") == provider:
                    return candidate
        return candidates[0]

    def _should_skip_tmdb_refresh_without_known_season(self, record: FollowingRecord) -> bool:
        if record.provider == "tmdb":
            return False
        tmdb_id = str(record.external_ids.get("tmdb") or "").strip()
        if not tmdb_id:
            return False
        media_kind = str(record.media_kind or "").strip().lower()
        if media_kind == "movie" or "电影" in media_kind:
            return False
        return record.season_number <= 0

    def _tmdb_refresh_candidate_from_record(self, record: FollowingRecord):
        normalized, season_number = self._normalized_tmdb_provider_id_and_season(
            record,
            season_number=record.season_number,
        )
        if not normalized:
            return None
        if normalized.startswith("tv:") and season_number <= 0:
            return None
        raw = {"season_number": season_number} if season_number > 0 and normalized.startswith("tv:") else {}
        return MetadataScrapeCandidate(
            provider="tmdb",
            provider_label="TMDB",
            provider_id=normalized,
            title=record.title,
            subtitle="电影" if normalized.startswith("movie:") else "剧集",
            raw=raw,
        )

    def _bangumi_refresh_candidate_from_record(self, record: FollowingRecord):
        provider_id = ""
        if record.provider == "bangumi":
            provider_id = record.provider_id
        if not provider_id:
            provider_id = str(record.external_ids.get("bangumi") or "").strip()
        provider_id = provider_id.strip()
        if not provider_id:
            return None
        if provider_id.isdigit():
            provider_id = f"subject:{provider_id}"
        if not provider_id.startswith("subject:"):
            return None
        return MetadataScrapeCandidate(
            provider="bangumi",
            provider_label="Bangumi",
            provider_id=provider_id,
            title=record.title,
            subtitle="动漫",
        )

    def _tmdb_detail_candidate_for_season(
        self,
        record: FollowingRecord,
        *,
        season_number: int,
    ) -> MetadataScrapeCandidate | None:
        normalized, normalized_season = self._normalized_tmdb_provider_id_and_season(
            record,
            season_number=season_number,
            allow_zero_season=True,
        )
        if not normalized or not normalized.startswith("tv:") or normalized_season < 0:
            return None
        return MetadataScrapeCandidate(
            provider="tmdb",
            provider_label="TMDB",
            provider_id=normalized,
            title=record.title,
            subtitle="剧集",
            raw={"season_number": normalized_season},
        )

    def _normalized_tmdb_provider_id_and_season(
        self,
        record: FollowingRecord,
        *,
        season_number: int,
        allow_zero_season: bool = False,
    ) -> tuple[str, int]:
        provider_id = ""
        if record.provider == "tmdb":
            provider_id = record.provider_id
        if not provider_id:
            provider_id = str(record.external_ids.get("tmdb") or "").strip()
        normalized, season_number = self._normalize_tmdb_refresh_provider_id(
            provider_id,
            media_kind=record.media_kind,
            season_number=season_number,
            allow_zero_season=allow_zero_season,
        )
        return normalized, season_number

    def _normalize_tmdb_refresh_provider_id(
        self,
        provider_id: str,
        *,
        media_kind: str,
        season_number: int,
        allow_zero_season: bool = False,
    ) -> tuple[str, int]:
        text = str(provider_id or "").strip()
        if not text:
            return "", 0
        normalized_kind = str(media_kind or "").strip().lower()
        if text.startswith("movie:"):
            return text, 0
        if text.startswith("tv:"):
            parts = text.split(":")
            if len(parts) >= 4 and parts[2] == "season":
                resolved_season = (
                    season_number
                    if season_number > 0 or (allow_zero_season and season_number == 0)
                    else self._to_int(parts[3])
                )
                if resolved_season < 0:
                    return f"tv:{parts[1]}", 0
                if resolved_season <= 0 and not allow_zero_season:
                    return f"tv:{parts[1]}", 0
                return f"tv:{parts[1]}:season:{resolved_season}", resolved_season
            if season_number > 0 or allow_zero_season:
                requested_season = max(0, int(season_number or 0))
                return f"{text}:season:{requested_season}", requested_season
            if season_number <= 0:
                return text, 0
            return f"{text}:season:{season_number}", season_number
        if not text.isdigit():
            return "", 0
        if normalized_kind == "movie" or "电影" in normalized_kind:
            return f"movie:{text}", 0
        if season_number > 0 or allow_zero_season:
            requested_season = max(0, int(season_number or 0))
            return f"tv:{text}:season:{requested_season}", requested_season
        if season_number <= 0:
            return f"tv:{text}", 0
        return f"tv:{text}:season:{season_number}", season_number

    def _ensure_metadata_bundle(
        self,
        record: FollowingRecord,
        snapshot: FollowingDetailSnapshot,
        *,
        force: bool = False,
        tmdb_detail_record: MetadataRecord | None = None,
    ) -> FollowingDetailSnapshot:
        if snapshot.metadata_bundle is not None and not force:
            return snapshot
        candidate = self._tmdb_refresh_candidate_from_record(record)
        if candidate is None and tmdb_detail_record is None:
            return self._ensure_single_source_metadata_bundle(record, snapshot, provider="bangumi")
        detail_record = tmdb_detail_record
        if detail_record is None:
            detail_record, _detail_error = load_candidate_detail_record_full(
                self._metadata_search_service,
                candidate,
            )
        if detail_record is None:
            return snapshot
        gateway = FollowingMetadataGateway(self._metadata_search_service)
        provider_records = gateway.load_source_records(record, tmdb_record=detail_record)
        _bundle, _merged_record, merged_snapshot = build_following_metadata_bundle(
            base_record=record,
            base_snapshot=snapshot,
            tmdb_detail_record=detail_record,
            provider_records=provider_records,
        )
        merged_snapshot.following_id = snapshot.following_id or record.id
        return merged_snapshot

    def _ensure_single_source_metadata_bundle(
        self,
        record: FollowingRecord,
        snapshot: FollowingDetailSnapshot,
        *,
        provider: str,
    ) -> FollowingDetailSnapshot:
        if provider != "bangumi":
            return snapshot
        candidate = self._bangumi_refresh_candidate_from_record(record)
        if candidate is None:
            return snapshot
        detail_record, _detail_error = load_candidate_detail_record(
            self._metadata_search_service,
            candidate,
        )
        if detail_record is None:
            return snapshot
        _bundle, _merged_record, merged_snapshot = build_following_source_metadata_bundle(
            base_record=record,
            base_snapshot=snapshot,
            provider="bangumi",
            provider_label="Bangumi",
            detail_record=detail_record,
            confidence=1.0,
        )
        merged_snapshot.following_id = snapshot.following_id or record.id
        return merged_snapshot

    def _merge_refreshed_snapshot(
        self,
        existing: FollowingDetailSnapshot,
        refreshed: FollowingDetailSnapshot,
    ) -> FollowingDetailSnapshot:
        merged = merge_following_snapshot(existing, refreshed)
        if not merged.episodes:
            return merged
        existing_by_key = {
            (episode.season_number, episode.episode_number): episode
            for episode in existing.episodes
        }
        ordered_keys: list[tuple[int, int]] = []
        refreshed_by_key: dict[tuple[int, int], FollowingEpisode] = {}
        for episode in merged.episodes:
            key = (episode.season_number, episode.episode_number)
            ordered_keys.append(key)
            refreshed_by_key[key] = episode
        for episode in existing.episodes:
            key = (episode.season_number, episode.episode_number)
            fallback_key = (0, episode.episode_number)
            if key in refreshed_by_key or fallback_key in refreshed_by_key:
                continue
            ordered_keys.append(key)

        episodes: list[FollowingEpisode] = []
        for key in ordered_keys:
            episode = refreshed_by_key.get(key)
            if episode is None and key[0] != 0:
                episode = refreshed_by_key.get((0, key[1]))
            existing_episode = existing_by_key.get(key) or existing_by_key.get((0, key[1]))
            if episode is None:
                if existing_episode is not None:
                    episodes.append(existing_episode)
                continue
            if existing_episode is None:
                episodes.append(episode)
                continue
            episodes.append(
                replace(
                    episode,
                    season_number=episode.season_number or existing_episode.season_number,
                    title=episode.title or existing_episode.title,
                    overview=episode.overview or existing_episode.overview,
                    air_date=episode.air_date or existing_episode.air_date,
                    still=episode.still or existing_episode.still,
                    runtime=episode.runtime or existing_episode.runtime,
                    is_special=episode.is_special or existing_episode.is_special,
                )
            )
        return replace(merged, episodes=episodes)

    def _latest_season_number(
        self,
        record: FollowingRecord,
        snapshot: FollowingDetailSnapshot | None = None,
    ) -> int:
        base = resolve_progress_season(
            record.season_number,
            record.latest_episode,
            fallback_season=record.season_number,
        )
        current_season = max(0, int(record.current_season_number or 0))
        if current_season > base:
            base = current_season
        if snapshot is None:
            return base
        snapshot_seasons = [int(season.season_number) for season in snapshot.seasons if int(season.season_number or 0) > 0]
        snapshot_seasons.extend(
            int(episode.season_number)
            for episode in snapshot.episodes
            if int(episode.season_number or 0) > 0 and not episode.is_special
        )
        if snapshot.next_episode is not None and int(snapshot.next_episode.season_number or 0) > 0:
            snapshot_seasons.append(int(snapshot.next_episode.season_number))
        if record.latest_episode > 0 and snapshot_seasons:
            return max(max(snapshot_seasons), base)
        return base

    @staticmethod
    def _display_total_episodes(record: FollowingRecord, *, completion_state: str) -> int:
        return resolve_display_total_episodes(
            total_episodes=record.total_episodes,
            latest_episode=record.latest_episode,
            completion_state=completion_state,
        )

    @staticmethod
    def _normalize_loaded_season_episode(
        *,
        season_number: int,
        episode_number: int,
        total_episodes: int,
        snapshot: FollowingDetailSnapshot,
        overflow_value: int | None,
    ) -> int:
        normalized_season = max(0, int(season_number or 0))
        normalized_episode = max(0, int(episode_number or 0))
        if normalized_season <= 0 or normalized_episode <= 0:
            return normalized_episode
        season_count = 0
        for season in snapshot.seasons:
            if int(season.season_number or 0) == normalized_season:
                season_count = max(0, int(season.episode_count or 0))
                break
        if season_count <= 0:
            return normalized_episode
        local_numbers = [
            int(episode.episode_number or 0)
            for episode in snapshot.episodes
            if int(episode.episode_number or 0) > 0
            and not episode.is_special
            and (
                int(episode.season_number or 0) or normalized_season
            ) == normalized_season
        ]
        if not local_numbers:
            return normalized_episode
        local_latest = max(local_numbers)
        if normalized_episode > local_latest and local_latest >= season_count:
            if overflow_value is None:
                normalized_total = max(0, int(total_episodes or 0))
                if normalized_total > season_count and normalized_episode <= normalized_total:
                    return normalized_episode
                next_episode = snapshot.next_episode
                next_season = int(next_episode.season_number or 0) if next_episode is not None else 0
                next_number = int(next_episode.episode_number or 0) if next_episode is not None else 0
                if next_season == normalized_season and next_number > season_count:
                    return normalized_episode
            return local_latest if overflow_value is None else max(0, int(overflow_value))
        return normalized_episode

    def _normalized_progress_record(
        self,
        record: FollowingRecord,
        snapshot: FollowingDetailSnapshot | None,
    ) -> FollowingRecord:
        if snapshot is None:
            return record
        current_season_number = resolve_progress_season(
            record.current_season_number,
            record.current_episode,
            fallback_season=record.season_number,
        )
        latest_season_number = self._latest_season_number(record, snapshot)
        current_episode = self._normalize_loaded_season_episode(
            season_number=current_season_number,
            episode_number=record.current_episode,
            total_episodes=record.total_episodes,
            snapshot=snapshot,
            overflow_value=0,
        )
        latest_episode = self._normalize_loaded_season_episode(
            season_number=latest_season_number,
            episode_number=record.latest_episode,
            total_episodes=record.total_episodes,
            snapshot=snapshot,
            overflow_value=None,
        )
        if (
            current_season_number == record.current_season_number
            and current_episode == record.current_episode
            and latest_episode == record.latest_episode
        ):
            return record
        return replace(
            record,
            current_season_number=current_season_number,
            current_episode=current_episode,
            latest_episode=latest_episode,
        )

    def _progress_text(
        self,
        record: FollowingRecord,
        *,
        snapshot: FollowingDetailSnapshot | None = None,
        completion_state: str | None = None,
        media_kind: str = "",
    ) -> str:
        if media_kind == "movie":
            return "已观看" if record.current_episode > 0 or record.last_played_at > 0 else "未观看"
        record = self._normalized_progress_record(record, snapshot)
        parts = []
        resolved_completion_state = completion_state or self._completion_state(record, snapshot)
        display_total = self._display_total_episodes(record, completion_state=resolved_completion_state)
        current_season_number = resolve_progress_season(
            record.current_season_number,
            record.current_episode,
            fallback_season=record.season_number,
        )
        latest_season_number = self._latest_season_number(record, snapshot)
        if (
            display_total > 0
            and resolved_completion_state == FollowingCompletionState.COMPLETED
            and record.latest_episode >= display_total
            and progress_at_or_beyond(
                current_season_number,
                record.current_episode,
                latest_season_number,
                display_total,
                current_fallback_season=record.season_number,
                latest_fallback_season=latest_season_number,
            )
        ):
            if latest_season_number > 0:
                return f"已看完 · S{latest_season_number}共 {display_total} 集 · 已完结"
            return f"已看完 · {display_total}集 · 已完结"
        current_text = format_progress_episode(
            "看到",
            current_season_number,
            record.current_episode,
            fallback_season=record.season_number,
        )
        if current_text:
            parts.append(current_text)
        if record.latest_episode > 0 and display_total > 0:
            latest_text = format_progress_episode(
                "最新",
                latest_season_number,
                record.latest_episode,
                fallback_season=record.season_number,
            )
            parts.append(f"{latest_text} / 总 {display_total}")
        elif record.latest_episode > 0:
            parts.append(
                format_progress_episode(
                    "最新",
                    latest_season_number,
                    record.latest_episode,
                    fallback_season=record.season_number,
                )
            )
        elif display_total > 0:
            parts.append(f"总 {display_total}")
        return " · ".join(parts) if parts else "进度未知"

    def _snapshot_needs_refresh(self, snapshot: FollowingDetailSnapshot) -> bool:
        return not any(
            (
                snapshot.overview,
                snapshot.metadata_fields,
                snapshot.cast,
                snapshot.crew,
                snapshot.episodes,
                snapshot.posters,
                snapshot.backdrops,
            )
        )

    def _snapshot_from_vod(
        self,
        vod: VodItem,
        playlist: list[PlayItem],
        *,
        metadata_raw_episodes: list[dict[str, object]] | None = None,
        refreshed_at: int,
    ) -> FollowingDetailSnapshot:
        metadata_episodes = [self._episode_from_raw(item) for item in metadata_raw_episodes or []]
        return FollowingDetailSnapshot(
            overview=str(vod.vod_content or "").strip(),
            metadata_fields=self._metadata_fields_from_vod(vod),
            cast=[{"name": name} for name in self._split_people(vod.vod_actor)],
            crew=[{"name": name, "job": "Director"} for name in self._split_people(vod.vod_director)],
            episodes=metadata_episodes or [
                FollowingEpisode(
                    episode_number=number,
                    title=str(
                        playlist_item.episode_display_title
                        or playlist_item.media_title
                        or playlist_item.original_title
                        or playlist_item.title
                        or ""
                    ).strip(),
                    still=str(playlist_item.video_cover_override or "").strip(),
                )
                for number, playlist_item in zip(self._playlist_episode_numbers(playlist), playlist, strict=False)
            ],
            posters=[source for source in [str(vod.vod_pic or "").strip(), *list(vod.poster_candidates or [])] if source],
            refreshed_at=refreshed_at,
        )

    def _snapshot_has_data(self, snapshot: FollowingDetailSnapshot) -> bool:
        return not self._snapshot_needs_refresh(snapshot)

    def _metadata_fields_from_vod(self, vod: VodItem) -> list[dict[str, str]]:
        fields: list[dict[str, str]] = []
        seen: set[str] = set()

        def put(label: str, value: object) -> None:
            normalized_label = str(label or "").strip()
            normalized_value = str(value or "").strip()
            if not normalized_label or not normalized_value or normalized_label in seen:
                return
            fields.append({"label": normalized_label, "value": normalized_value})
            seen.add(normalized_label)

        put("类型", vod.type_name or vod.category_name)
        put("年代", vod.vod_year)
        put("地区", vod.vod_area)
        put("语言", vod.vod_lang)
        put("导演", vod.vod_director)
        put("演员", vod.vod_actor)
        if int(vod.dbid or 0):
            put("豆瓣ID", str(int(vod.dbid or 0)))

        internal_labels = {
            "episodes",
            "last_episode_to_air",
            "next_episode_to_air",
            "seasons",
            "number_of_episodes",
            "number_of_seasons",
            "last_air_date",
            "watch_provider_sources",
        }
        for field in list(vod.detail_fields or []):
            label = str(getattr(field, "label", "") or "").strip()
            if not label or label.lower() in internal_labels:
                continue
            put(label, getattr(field, "value", ""))
        return fields

    def _split_people(self, value: object) -> list[str]:
        return [part.strip() for part in re.split(r"[,/、]", str(value or "")) if part.strip()]

    def _playlist_episode_numbers(self, playlist: list[PlayItem]) -> list[int]:
        if len(playlist) > 1:
            return list(range(1, len(playlist) + 1))
        return [
            number
            for playlist_item in playlist
            if (number := infer_playlist_episode_number(playlist_item, playlist) or 0) > 0
        ]

    def _playlist_position(self, item: PlayItem, playlist: list[PlayItem]) -> int:
        if len(playlist) <= 1:
            return 0
        for index, playlist_item in enumerate(playlist, start=1):
            if playlist_item is item:
                return index
        for index, playlist_item in enumerate(playlist, start=1):
            if playlist_item.url == item.url and playlist_item.title == item.title:
                return index
        item_index = int(getattr(item, "index", 0) or 0)
        return item_index + 1 if 0 <= item_index < len(playlist) else 0

    def _metadata_raw_episodes_from_vod(self, vod: VodItem) -> list[dict[str, object]]:
        for field in list(vod.detail_fields or []):
            if str(getattr(field, "label", "") or "").strip().lower() != "episodes":
                continue
            value = getattr(field, "value", "")
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            text = str(value or "").strip()
            if not text:
                continue
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                continue
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        return []

    def _metadata_total_episodes_from_vod(self, vod: VodItem) -> int:
        for field in list(vod.detail_fields or []):
            label = str(getattr(field, "label", "") or "").strip().lower()
            if label not in {"话数", "集数", "总集数", "episodes_total", "total episodes"}:
                continue
            match = re.search(r"\d+", str(getattr(field, "value", "") or ""))
            if match:
                return int(match.group(0))
        return 0

    def _episode_from_raw(self, raw: dict[str, object]) -> FollowingEpisode:
        number = self._to_int(raw.get("episode_number") or raw.get("sort") or raw.get("ep"))
        return FollowingEpisode(
            episode_number=number,
            season_number=self._to_int(raw.get("season_number")),
            title=str(raw.get("name_cn") or raw.get("name") or raw.get("long_title") or raw.get("title") or "").strip(),
            overview=str(raw.get("overview") or raw.get("desc") or raw.get("summary") or "").strip(),
            air_date=str(raw.get("air_date") or raw.get("airdate") or raw.get("date") or "").strip(),
            still=str(raw.get("still_url") or raw.get("still") or raw.get("cover") or raw.get("image") or "").strip(),
            runtime=self._to_int(raw.get("runtime") or raw.get("duration")),
            is_special=number <= 0 or self._to_int(raw.get("type")) != 0,
        )

    def _to_int(self, value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _external_ids_from_vod(self, vod: VodItem, item: PlayItem) -> dict[str, str]:
        external_ids = {"douban": str(vod.dbid)} if int(vod.dbid or 0) else {}
        for field in [*list(vod.detail_fields or []), *list(item.detail_fields or [])]:
            label = str(getattr(field, "label", "") or "").strip().lower()
            value = str(getattr(field, "value", "") or "").strip()
            if not value:
                continue
            if "tmdb" in label:
                external_ids["tmdb"] = value
            elif "bangumi" in label:
                external_ids["bangumi"] = value
            elif "豆瓣" in label or "douban" in label:
                external_ids["douban"] = value
        return external_ids
