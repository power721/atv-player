from __future__ import annotations

import logging
import re
from dataclasses import replace
from urllib.parse import urlparse

from atv_player.episode_titles import extract_season_number
from atv_player.metadata.async_runner import run_provider_detail, run_provider_searches
from atv_player.metadata.base import MetadataProvider
from atv_player.metadata.bindings import (
    bilibili_season_binding_title,
    is_query_redirect_binding,
)
from atv_player.metadata.cache import MetadataCache
from atv_player.metadata.cache_key import provider_search_cache_key
from atv_player.metadata.matching import (
    _MIN_CONFIDENT_MATCH_SCORE,
    is_confident_match,
    normalize_match_title,
    score_match,
    strip_match_season_suffix,
)
from atv_player.metadata.query import is_pure_season_title
from atv_player.metadata.merge import (
    choose_preferred_title,
    fill_missing_metadata_record,
    merge_metadata_record,
    override_visual_metadata_record,
)
from atv_player.metadata.models import MetadataContext, MetadataMatch, MetadataRecord
from atv_player.metadata.query import normalize_metadata_title
from atv_player.models import VodItem

logger = logging.getLogger(__name__)

_SEARCH_CACHE_TTL_SECONDS = 7 * 24 * 3600
_EMPTY_SEARCH_CACHE_TTL_SECONDS = 3600
_DETAIL_CACHE_TTL_SECONDS = 7 * 24 * 3600
_LOCAL_DOUBAN_PRIME_SOURCE_KINDS = {"telegram", "emby", "jellyfin"}
_REMOTE_AUTO_SEARCH_IGNORE_DBID_SOURCE_KINDS = {"telegram", "emby", "jellyfin"}
_LOCAL_DOUBAN_PROVIDER_NAMES = {"local_douban", "remote_douban"}
_DOUBAN_ID_PROVIDER_NAMES = {"official_douban", "local_douban", "douban"}
_PLUGIN_PLATFORM_HIGH_CONFIDENCE_REQUIRED_PROVIDERS = {
    "bilibili",
    "iqiyi",
    "mgtv",
    "sohu",
    "tencent",
    "youku",
}
_ANIME_MARKERS = ("动漫", "动画", "番剧", "anime", "acg", "国创", "声优")
_LIVE_ACTION_MARKERS = ("电视剧", "剧集", "连续剧", "真人", "古装", "短剧")
_MOVIE_MARKERS = ("电影", "影片", "movie")
_AUTHORITATIVE_ID_MATCH_SCORE = 2.0
_BILIBILI_SS_ID_RE = re.compile(r"^ss(\d+)$", re.IGNORECASE)
_BILIBILI_SEASON_ID_RE = re.compile(r"^season\$(\d+)$", re.IGNORECASE)
_TMDB_SEASON_PROVIDER_ID_RE = re.compile(r"^tv:[^:]+:season:(\d+)$")


def _iter_category_values(value: object) -> list[str]:
    if isinstance(value, dict):
        return _iter_category_values(value.get("value"))
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(_iter_category_values(item))
        return values
    text = str(value or "").strip()
    return [text] if text else []


def _iter_delimited_values(value: object) -> list[str]:
    if isinstance(value, dict):
        return _iter_delimited_values(value.get("value"))
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(_iter_delimited_values(item))
        return values
    return [
        token.strip()
        for token in re.split(r"[,/|、，]", str(value or ""))
        if token.strip()
    ]


def _normalized_detail_tokens(value: object) -> set[str]:
    return {
        normalized
        for token in _iter_delimited_values(value)
        if (normalized := normalize_match_title(token))
    }


def _classify_media_kind(*values: object) -> str:
    tokens = " ".join(
        token.strip().lower()
        for value in values
        for token in _iter_category_values(value)
        if token and token.strip()
    )
    if not tokens:
        return ""
    if any(marker in tokens for marker in _ANIME_MARKERS):
        return "anime"
    if any(marker in tokens for marker in _MOVIE_MARKERS):
        return "movie"
    if any(marker in tokens for marker in _LIVE_ACTION_MARKERS):
        return "live_action"
    return ""


def _match_media_kind(match: MetadataMatch) -> str:
    if match.provider == "bangumi":
        return "anime"
    if match.provider == "tmdb":
        provider_id = str(match.provider_id or "").strip()
        if provider_id.startswith("movie:"):
            return "movie"
    raw = dict(match.raw or {})
    return _classify_media_kind(
        raw.get("typeName"),
        raw.get("channel"),
        raw.get("genres"),
        raw.get("categories"),
        raw.get("baseTags"),
        raw.get("category"),
        match.title,
    )


def _record_media_kind(record: MetadataRecord) -> str:
    if record.provider == "bangumi":
        return "anime"
    if record.provider == "tmdb":
        provider_id = str(record.provider_id or "").strip()
        if provider_id.startswith("movie:"):
            return "movie"
    return _classify_media_kind(
        record.genres,
        record.title,
        record.original_title,
        record.country,
        record.detail_fields,
    )


def _vod_media_kind(vod: VodItem) -> str:
    return _classify_media_kind(vod.type_name, vod.category_name)


def _media_kinds_compatible(current_kind: str, candidate_kind: str) -> bool:
    if not current_kind or not candidate_kind:
        return True
    return current_kind == candidate_kind


def _record_has_enrichment_data(record: MetadataRecord) -> bool:
    return any(
        (
            str(record.poster or "").strip(),
            str(record.backdrop or "").strip(),
            str(record.overview or "").strip(),
            str(record.rating or "").strip(),
            list(record.actors or []),
            list(record.directors or []),
            list(record.genres or []),
            str(record.country or "").strip(),
            str(record.language or "").strip(),
            list(record.aliases or []),
            str(record.imdb_id or "").strip(),
            str(record.tmdb_id or "").strip(),
            int(record.douban_id or 0),
            list(record.detail_fields or []),
        )
    )


def _iqiyi_match_raw_has_detail(match: MetadataMatch) -> bool:
    raw = dict(match.raw or {})
    return any(
        (
            str(raw.get("promptDesc") or "").strip(),
            str(raw.get("introduction") or "").strip(),
            str(raw.get("channel") or "").strip(),
            list(raw.get("metaTags") or []),
            list(raw.get("baseTags") or []),
            raw.get("category"),
            raw.get("region"),
            raw.get("language"),
            raw.get("directors"),
            raw.get("actors"),
        )
    )


def _should_refresh_cached_detail(provider_name: str, cached: MetadataRecord, match: MetadataMatch) -> bool:
    if provider_name != "iqiyi":
        return False
    if _record_has_enrichment_data(cached):
        return False
    return _iqiyi_match_raw_has_detail(match)


def _should_promote_sohu_record(match: MetadataMatch) -> bool:
    if match.provider != "sohu":
        return False
    return bool(dict(match.raw or {}).get("sohu_preferred_over_tmdb"))


def _record_conflicts_with_plugin_original(query, record: MetadataRecord) -> bool:
    if str(getattr(query, "source_kind", "") or "").strip() != "plugin":
        return False
    query_people = _normalized_detail_tokens(getattr(query, "vod_director", ""))
    query_people.update(_normalized_detail_tokens(getattr(query, "vod_actor", "")))
    record_people = _normalized_detail_tokens(record.directors)
    record_people.update(_normalized_detail_tokens(record.actors))
    if not query_people or not record_people or query_people & record_people:
        return False
    query_countries = _normalized_detail_tokens(getattr(query, "vod_area", ""))
    record_countries = _normalized_detail_tokens(record.country)
    return bool(query_countries and record_countries and not (query_countries & record_countries))


def _year_values_compatible(left: object, right: object) -> bool:
    left_year = _parse_year_number(left)
    right_year = _parse_year_number(right)
    if left_year is None or right_year is None:
        return True
    return abs(left_year - right_year) < 2


def _title_values_strongly_aligned(query_title: object, match: MetadataMatch, record: MetadataRecord) -> bool:
    normalized_query = normalize_match_title(query_title)
    candidates = [match.title, record.title, record.original_title]
    if any(normalized_query and normalized_query == normalize_match_title(candidate) for candidate in candidates):
        return True
    query_base = normalize_match_title(strip_match_season_suffix(query_title))
    return bool(
        query_base
        and any(
            query_base == normalize_match_title(strip_match_season_suffix(candidate))
            for candidate in candidates
            if str(candidate or "").strip()
        )
    )


def _record_strongly_matches_plugin_original_people(query, match: MetadataMatch, record: MetadataRecord) -> bool:
    if not _title_values_strongly_aligned(query.title, match, record):
        return False
    if not _year_values_compatible(query.year, match.year or record.year):
        return False
    query_countries = _normalized_detail_tokens(getattr(query, "vod_area", ""))
    record_countries = _normalized_detail_tokens(record.country)
    if query_countries and record_countries and not (query_countries & record_countries):
        return False

    query_people = _normalized_detail_tokens(getattr(query, "vod_director", ""))
    query_people.update(_normalized_detail_tokens(getattr(query, "vod_actor", "")))
    record_people = _normalized_detail_tokens(record.directors)
    record_people.update(_normalized_detail_tokens(record.actors))
    if not query_people or not record_people:
        return False
    overlap = query_people & record_people
    required_overlap = min(3, len(query_people))
    if len(overlap) < required_overlap:
        return False
    record_coverage = len(overlap) / len(record_people)
    query_coverage = len(overlap) / len(query_people)
    return record_coverage >= 0.8 and query_coverage >= 0.6


def _canonical_identity_url(value: object) -> str:
    text = str(value or "").strip()
    if not text.startswith(("http://", "https://")):
        return ""
    parsed = urlparse(text)
    host = parsed.netloc.lower()
    path = re.sub(r"/+$", "", parsed.path)
    if not host or not path:
        return ""
    return f"{host}{path}"


def _iter_detail_field_identity_values(fields: object):
    for field in list(fields or []):
        yield getattr(field, "value", "")
        for part in list(getattr(field, "value_parts", []) or []):
            yield getattr(part, "label", "")
            action = getattr(part, "action", None)
            if action is not None:
                yield getattr(action, "value", "")


def _context_identity_urls(context: MetadataContext) -> set[str]:
    vod = context.vod
    values: list[object] = [
        context.source_key,
        vod.vod_id,
        vod.vod_play_url,
        vod.path,
    ]
    values.extend(_iter_detail_field_identity_values(vod.detail_fields))
    current_item = context.current_item
    if current_item is not None:
        values.extend(
            [
                current_item.url,
                current_item.original_url,
                current_item.vod_id,
                current_item.path,
                current_item.play_source,
            ]
        )
        values.extend(_iter_detail_field_identity_values(current_item.detail_fields))
    return {url for value in values if (url := _canonical_identity_url(value))}


def _record_identity_urls(match: MetadataMatch, record: MetadataRecord) -> set[str]:
    values: list[object] = [match.provider_id, record.provider_id]
    for item in list(record.detail_fields or []):
        if isinstance(item, dict):
            values.append(item.get("value"))
            for part in list(item.get("value_parts") or []):
                values.append(getattr(part, "label", ""))
                action = getattr(part, "action", None)
                if action is not None:
                    values.append(getattr(action, "value", ""))
    return {url for value in values if (url := _canonical_identity_url(value))}


def _record_has_authoritative_identity_match(context: MetadataContext, record: MetadataRecord) -> bool:
    vod = context.vod
    if record.douban_id and int(getattr(vod, "dbid", 0) or 0) == record.douban_id:
        return True
    existing_ids: dict[str, set[str]] = {"TMDB ID": set(), "IMDb ID": set()}
    for field in list(getattr(vod, "detail_fields", []) or []):
        label = str(getattr(field, "label", "") or "").strip()
        if label in existing_ids:
            existing_ids[label].add(str(getattr(field, "value", "") or "").strip())
    return bool(
        (record.tmdb_id and str(record.tmdb_id) in existing_ids["TMDB ID"])
        or (record.imdb_id and str(record.imdb_id) in existing_ids["IMDb ID"])
    )


def _plugin_query_has_original_metadata_anchors(query) -> bool:
    return any(
        str(getattr(query, field_name, "") or "").strip()
        for field_name in ("vod_area", "vod_lang", "vod_director", "vod_actor")
    )


def _plugin_platform_record_is_high_confidence(
    context: MetadataContext,
    query,
    match: MetadataMatch,
    record: MetadataRecord,
) -> bool:
    if str(getattr(query, "source_kind", "") or "").strip() != "plugin":
        return True
    if record.provider not in _PLUGIN_PLATFORM_HIGH_CONFIDENCE_REQUIRED_PROVIDERS:
        return True
    if not _plugin_query_has_original_metadata_anchors(query):
        return True
    if _context_identity_urls(context) & _record_identity_urls(match, record):
        return True
    if _record_has_authoritative_identity_match(context, record):
        return True
    return _record_strongly_matches_plugin_original_people(query, match, record)


def _sync_cleaned_query_title(vod: VodItem, query) -> None:
    current_title = str(vod.vod_name or "").strip()
    query_title = str(getattr(query, "title", "") or "").strip()
    if not current_title or not query_title or current_title == query_title:
        return
    if normalize_metadata_title(current_title) == query_title:
        vod.vod_name = query_title


def _parse_year_number(value: object) -> int | None:
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    if match is None:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _tmdb_first_season_binding_year_conflicts(query, provider_id: object, record: MetadataRecord) -> bool:
    if record.provider != "tmdb":
        return False
    provider_id_text = str(provider_id or record.provider_id or "").strip()
    match = _TMDB_SEASON_PROVIDER_ID_RE.match(provider_id_text)
    if match is None or match.group(1) != "1":
        return False
    query_season = extract_season_number(getattr(query, "title", ""))
    if query_season not in (None, 1):
        return False
    query_year = _parse_year_number(getattr(query, "year", ""))
    record_year = _parse_year_number(record.year)
    return bool(query_year is not None and record_year is not None and abs(query_year - record_year) >= 2)


def _is_authoritative_douban_id_match(query, match: MetadataMatch) -> bool:
    vod_dbid = int(getattr(query, "vod_dbid", 0) or 0)
    if vod_dbid <= 0:
        return False
    if match.provider not in _DOUBAN_ID_PROVIDER_NAMES:
        return False
    return str(match.provider_id or "").strip() == str(vod_dbid)


def _bilibili_season_id_from_vod_id(vod_id: object) -> str:
    text = str(vod_id or "").strip()
    for pattern in (_BILIBILI_SS_ID_RE, _BILIBILI_SEASON_ID_RE):
        match = pattern.match(text)
        if match is not None:
            return match.group(1)
    return ""


def _bilibili_season_id_from_detail_fields(fields: object) -> str:
    for field in list(fields or []):
        label = str(getattr(field, "label", "") or "").strip().lower()
        if label != "season id":
            continue
        value_parts = list(getattr(field, "value_parts", []) or [])
        for part in value_parts:
            action = getattr(part, "action", None)
            action_value = str(getattr(action, "value", "") or "").strip()
            season_id = _bilibili_season_id_from_vod_id(action_value)
            if season_id:
                return season_id
            part_label = str(getattr(part, "label", "") or "").strip()
            if part_label.isdigit():
                return part_label
        value = str(getattr(field, "value", "") or "").strip()
        season_id = _bilibili_season_id_from_vod_id(value)
        if season_id:
            return season_id
        if value.isdigit():
            return value
    return ""


def _bilibili_season_id_from_vod(vod: VodItem) -> str:
    return _bilibili_season_id_from_vod_id(vod.vod_id) or _bilibili_season_id_from_detail_fields(vod.detail_fields)


class MetadataHydrator:
    def __init__(
        self,
        cache: MetadataCache,
        providers: list[MetadataProvider],
        binding_repository=None,
    ) -> None:
        self._cache = cache
        self._providers = providers
        self._providers_by_name = {provider.name: provider for provider in providers}
        self._binding_repository = binding_repository

    def _load_bound_record(self, context: MetadataContext, query):
        if self._binding_repository is None:
            return None
        binding = None
        binding_key: tuple[object, object] | None = None
        if query.source_kind == "bilibili":
            season_binding_title = bilibili_season_binding_title(_bilibili_season_id_from_vod(context.vod))
            if season_binding_title:
                binding = self._binding_repository.load(season_binding_title, "")
                if is_query_redirect_binding(binding):
                    binding = None
                if binding is not None:
                    binding_key = (season_binding_title, "")
        if binding is None:
            binding = self._binding_repository.load(query.title, query.year)
            if is_query_redirect_binding(binding):
                binding = None
            if binding is not None:
                binding_key = (query.title, query.year)
        if binding is None and query.source_kind == "bilibili" and not str(query.year or "").strip():
            load_by_title = getattr(self._binding_repository, "load_by_title", None)
            if callable(load_by_title):
                binding = load_by_title(query.title)
                if is_query_redirect_binding(binding):
                    binding = None
                if binding is not None:
                    binding_key = (binding.normalized_title, binding.normalized_year)
        if binding is None:
            return None
        if binding_key is None:
            binding_key = (query.title, query.year)
        provider = self._providers_by_name.get(binding.provider)
        if provider is None:
            self._binding_repository.delete(*binding_key)
            return None
        detail_cache_key = self._provider_detail_cache_key(provider, binding.provider_id)
        cached = self._cache.load_detail(
            binding.provider,
            detail_cache_key,
            ttl_seconds=_DETAIL_CACHE_TTL_SECONDS,
        )
        if cached is not None:
            if _tmdb_first_season_binding_year_conflicts(query, binding.provider_id, cached):
                self._binding_repository.delete(*binding_key)
                self._cache.delete_detail(binding.provider, detail_cache_key)
                return None
            return cached
        try:
            record = provider.get_detail(
                MetadataMatch(
                    provider=binding.provider,
                    provider_id=binding.provider_id,
                    title=binding.matched_title or query.title,
                    year=binding.matched_year or query.year,
                )
            )
        except Exception:
            self._binding_repository.delete(*binding_key)
            return None
        if _tmdb_first_season_binding_year_conflicts(query, binding.provider_id, record):
            self._binding_repository.delete(*binding_key)
            return None
        self._cache.save_detail(binding.provider, detail_cache_key, record)
        return record

    @staticmethod
    def _provider_search_cache_key(provider: MetadataProvider, query) -> tuple[str, str]:
        return provider_search_cache_key(provider, query)

    @staticmethod
    def _provider_detail_cache_key(provider: MetadataProvider, provider_id: object) -> str:
        detail_cache_key = getattr(provider, "detail_cache_key", None)
        if callable(detail_cache_key):
            return str(detail_cache_key(str(provider_id or "")))
        return str(provider_id or "")

    @staticmethod
    def _score_matches(query, matches: list[MetadataMatch]) -> list[MetadataMatch]:
        degenerate_query = is_pure_season_title(query.title)
        scored_matches: list[MetadataMatch] = []
        for match in matches:
            authoritative = _is_authoritative_douban_id_match(query, match)
            score = (
                _AUTHORITATIVE_ID_MATCH_SCORE
                if authoritative
                else score_match(query, match)
            )
            score = max(float(match.score or 0.0), score)
            if degenerate_query and not authoritative:
                # 纯季标记查询("S01")没有剧名可比,供应商自己的相关性分同样
                # 不可信;压到置信线以下,除非用户绑定/权威 ID 直指该条目。
                score = min(score, _MIN_CONFIDENT_MATCH_SCORE - 0.01)
            scored_matches.append(replace(match, score=score))
        return scored_matches

    def _load_provider_matches(self, provider: MetadataProvider, query) -> list[MetadataMatch]:
        cache_title, cache_year = self._provider_search_cache_key(provider, query)
        matches = self._cache.load_search(
            provider.name,
            cache_title,
            cache_year,
            ttl_seconds=_SEARCH_CACHE_TTL_SECONDS,
            empty_ttl_seconds=_EMPTY_SEARCH_CACHE_TTL_SECONDS,
        )
        if matches is None:
            result = run_provider_searches([provider], query, max_concurrency=1)[0]
            if result.error is not None:
                logger.warning(
                    "Metadata provider search failed provider=%s",
                    provider.name,
                    exc_info=result.error,
                    extra={"log_category": "metadata", "log_source": "app"},
                )
                return []
            matches = result.matches
            self._cache.save_search(provider.name, cache_title, cache_year, matches)
        return self._score_matches(query, list(matches))

    def _load_detail_record(self, provider: MetadataProvider, match: MetadataMatch):
        detail_cache_key = self._provider_detail_cache_key(provider, match.provider_id)
        cached = self._cache.load_detail(
            provider.name,
            detail_cache_key,
            ttl_seconds=_DETAIL_CACHE_TTL_SECONDS,
        )
        if cached is not None and not _should_refresh_cached_detail(provider.name, cached, match):
            return cached
        try:
            record = run_provider_detail(provider, match)
        except Exception as exc:
            logger.warning("Metadata provider detail failed provider=%s", provider.name, exc_info=exc)
            return None
        self._cache.save_detail(provider.name, detail_cache_key, record)
        return record

    def _load_bilibili_source_record(self, context: MetadataContext, query):
        if context.source_kind != "bilibili":
            return None
        season_id = _bilibili_season_id_from_vod(context.vod)
        if not season_id:
            return None
        provider = self._providers_by_name.get("bilibili")
        if provider is None:
            return None
        provider_id = f"https://www.bilibili.com/bangumi/play/ss{season_id}"
        match = MetadataMatch(
            provider="bilibili",
            provider_id=provider_id,
            title=str(query.title or "").strip(),
            year=str(query.year or "").strip(),
            raw={"provider_id": provider_id, "season_id": season_id},
        )
        return self._load_detail_record(provider, match)

    def _prepare_search_query(self, context: MetadataContext, query):
        if context.source_kind not in _REMOTE_AUTO_SEARCH_IGNORE_DBID_SOURCE_KINDS:
            return query
        if not str(query.title or "").strip():
            return query
        if int(query.vod_dbid or 0) <= 0:
            return query
        return replace(query, vod_dbid=0)

    def _prime_local_douban_query(self, context: MetadataContext, vod: VodItem, query, eligible_providers: list[MetadataProvider]):
        if context.source_kind not in _LOCAL_DOUBAN_PRIME_SOURCE_KINDS:
            return query
        local_provider = next((provider for provider in eligible_providers if provider.name in _LOCAL_DOUBAN_PROVIDER_NAMES), None)
        if local_provider is None:
            return query
        matches = self._load_provider_matches(local_provider, query)
        if not matches:
            return query
        best_match = max(matches, key=lambda item: item.score)
        if not is_confident_match(best_match.score):
            return query
        record = self._load_detail_record(local_provider, best_match)
        if record is None:
            return query
        preferred_title = choose_preferred_title(query.title, record.title)
        merge_metadata_record(vod, record, provider_priority=[item.name for item in self._providers])
        if preferred_title:
            vod.vod_name = preferred_title
        return replace(
            query,
            title=preferred_title or str(vod.vod_name or "").strip() or query.title,
            year=str(vod.vod_year or "").strip() or query.year,
            type_name=str(vod.type_name or "").strip() or query.type_name,
            category_name=str(vod.category_name or "").strip() or query.category_name,
        )

    def _apply_saved_query_redirect(self, query):
        # 用户在刮削对话框点"自动刮削"修正过查询标题时，会存一条查询重定向；自动
        # 水合按原始标题（如网盘混淆目录名）搜不到任何东西，先跳到修正后的标题。
        if self._binding_repository is None:
            return query
        load_redirect = getattr(self._binding_repository, "load_query_redirect", None)
        if not callable(load_redirect):
            return query
        redirect_title = str(query.title or "").strip()
        if not redirect_title:
            return query
        try:
            redirect = load_redirect(query.title, query.year)
        except Exception:
            return query
        if redirect is None:
            return query
        new_title, new_year = redirect
        new_title = str(new_title or "").strip()
        new_year = str(new_year or "").strip()
        if not new_title:
            return query
        if new_title == redirect_title and new_year == str(query.year or "").strip():
            return query
        return replace(query, title=new_title, year=new_year or query.year)

    def hydrate(self, context: MetadataContext) -> VodItem:
        vod = replace(context.vod)
        query = self._prepare_search_query(context, context.to_query())
        query = self._apply_saved_query_redirect(query)
        bound_record = self._load_bound_record(context, query)
        if bound_record is not None:
            merge_metadata_record(vod, bound_record, provider_priority=[item.name for item in self._providers])
            if bound_record.title:
                vod.vod_name = bound_record.title
            else:
                _sync_cleaned_query_title(vod, query)
            return vod
        primary_applied = False
        authoritative_primary_applied = False
        primary_kind = ""
        bilibili_source_record = self._load_bilibili_source_record(context, query)
        if bilibili_source_record is not None:
            merge_metadata_record(vod, bilibili_source_record, provider_priority=[item.name for item in self._providers])
            if bilibili_source_record.title:
                vod.vod_name = bilibili_source_record.title
            primary_applied = True
            primary_kind = _record_media_kind(bilibili_source_record) or _vod_media_kind(vod)
            context = replace(context, vod=vod)
            query = self._prepare_search_query(context, context.to_query())
        eligible_providers = [
            provider
            for provider in self._providers
            if not (bilibili_source_record is not None and provider.name == "bilibili")
            and provider.can_enrich(context)
        ]
        if not eligible_providers:
            return vod
        query = self._prime_local_douban_query(context, vod, query, eligible_providers)
        providers_needing_fetch: list[MetadataProvider] = []
        cache_keys_by_provider: list[tuple[str, str]] = []
        matches_by_provider: dict[int, list[MetadataMatch]] = {}
        for provider in eligible_providers:
            cache_title, cache_year = self._provider_search_cache_key(provider, query)
            cached_matches = self._cache.load_search(
                provider.name,
                cache_title,
                cache_year,
                ttl_seconds=_SEARCH_CACHE_TTL_SECONDS,
                empty_ttl_seconds=_EMPTY_SEARCH_CACHE_TTL_SECONDS,
            )
            if cached_matches is None:
                providers_needing_fetch.append(provider)
                cache_keys_by_provider.append((cache_title, cache_year))
                continue
            matches_by_provider[id(provider)] = self._score_matches(query, list(cached_matches))
        if providers_needing_fetch:
            search_results = run_provider_searches(
                providers_needing_fetch,
                query,
                max_concurrency=max(1, len(providers_needing_fetch)),
            )
            for provider, (cache_title, cache_year), result in zip(
                providers_needing_fetch,
                cache_keys_by_provider,
                search_results,
            ):
                if result.error is not None:
                    logger.warning(
                        "Metadata provider search failed provider=%s",
                        provider.name,
                        exc_info=result.error,
                        extra={"log_category": "metadata", "log_source": "app"},
                    )
                    matches_by_provider[id(provider)] = []
                    continue
                self._cache.save_search(provider.name, cache_title, cache_year, result.matches)
                matches_by_provider[id(provider)] = self._score_matches(query, list(result.matches))

        ranked_candidates: list[tuple[int, MetadataProvider, MetadataMatch]] = []
        for order, provider in enumerate(eligible_providers):
            matches = matches_by_provider.get(id(provider), [])
            if not matches:
                continue
            best_match = max(matches, key=lambda item: item.score)
            if not is_confident_match(best_match.score):
                continue
            ranked_candidates.append((order, provider, best_match))

        for order, provider, match in sorted(
            ranked_candidates,
            key=lambda item: (-item[2].score, item[0]),
        ):
            del order
            if (
                authoritative_primary_applied
                and not _is_authoritative_douban_id_match(query, match)
            ):
                corrected_query = replace(
                    query,
                    title=str(vod.vod_name or "").strip() or query.title,
                    year=str(vod.vod_year or "").strip() or query.year,
                    vod_dbid=0,
                )
                corrected_score = score_match(
                    corrected_query,
                    replace(match, score=0.0),
                )
                if not is_confident_match(corrected_score):
                    logger.info(
                        "Skip metadata candidate after authoritative Douban ID correction "
                        "provider=%s title=%s corrected_title=%s",
                        provider.name,
                        match.title,
                        corrected_query.title,
                    )
                    continue
            current_kind = primary_kind or _vod_media_kind(vod)
            if primary_applied and not _media_kinds_compatible(current_kind, _match_media_kind(match)):
                logger.info(
                    "Skip incompatible metadata candidate provider=%s title=%s current_kind=%s candidate_kind=%s",
                    provider.name,
                    match.title,
                    current_kind,
                    _match_media_kind(match),
                )
                continue
            record = self._load_detail_record(provider, match)
            if record is None:
                continue
            if not _plugin_platform_record_is_high_confidence(context, query, match, record):
                logger.info(
                    "Skip low-confidence plugin platform metadata provider=%s title=%s score=%s",
                    provider.name,
                    record.title or match.title,
                    match.score,
                    extra={"log_category": "metadata", "log_source": "app"},
                )
                continue
            if _record_conflicts_with_plugin_original(query, record):
                logger.info(
                    "Skip metadata record conflicting with plugin original details "
                    "provider=%s title=%s country=%s",
                    provider.name,
                    record.title,
                    record.country,
                    extra={"log_category": "metadata", "log_source": "app"},
                )
                continue
            if not primary_applied:
                merge_metadata_record(vod, record, provider_priority=[item.name for item in self._providers])
                if _is_authoritative_douban_id_match(query, match) and record.title:
                    vod.vod_name = record.title
                    authoritative_primary_applied = True
                else:
                    _sync_cleaned_query_title(vod, query)
                primary_applied = True
                primary_kind = _record_media_kind(record) or _match_media_kind(match) or _vod_media_kind(vod)
                continue
            current_kind = primary_kind or _vod_media_kind(vod)
            if not _media_kinds_compatible(current_kind, _record_media_kind(record)):
                logger.info(
                    "Skip incompatible metadata record provider=%s title=%s current_kind=%s candidate_kind=%s",
                    provider.name,
                    record.title,
                    current_kind,
                    _record_media_kind(record),
                )
                continue
            if _should_promote_sohu_record(match):
                merge_metadata_record(vod, record, provider_priority=[item.name for item in self._providers])
                override_visual_metadata_record(vod, record)
                continue
            fill_missing_metadata_record(vod, record)
            override_visual_metadata_record(vod, record)
        return vod
