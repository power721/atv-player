from __future__ import annotations

from dataclasses import replace
import logging
import re

from atv_player.danmaku.errors import DanmakuEmptyResultError, ProviderNotSupportedError
from atv_player.danmaku.models import DanmakuSearchItem, DanmakuSourceGroup, DanmakuSourceOption, DanmakuSourceSearchResult
from atv_player.danmaku.providers import (
    BilibiliDanmakuProvider,
    IqiyiDanmakuProvider,
    MgtvDanmakuProvider,
    TencentDanmakuProvider,
    YoukuDanmakuProvider,
)
from atv_player.danmaku.providers.base import DanmakuProvider
from atv_player.danmaku.utils import (
    build_xml,
    episode_title_matches,
    extract_episode_number,
    has_explicit_episode_marker,
    match_provider,
    normalize_name,
    should_filter_name,
    similarity_score,
    strip_episode_suffix,
)


logger = logging.getLogger(__name__)

_PREFERRED_MOVIE_VARIANT_TOKENS = (
    "原声版",
    "普通话版",
    "普通话",
    "国语版",
    "国语",
    "粤语版",
    "粤语",
    "臻彩",
)

_SUPPLEMENTAL_MOVIE_TOKENS = (
    "独家采访",
    "采访",
    "剧情速看",
    "速看",
    "深度剖析",
    "剖析",
    "揭秘",
    "解读",
    "解析",
    "预告",
    "花絮",
    "特辑",
    "片段",
    "幕后",
    "专访",
)

_MIN_DANMAKU_CANDIDATE_DURATION_SECONDS = 300
_LONG_FORM_DURATION_SECONDS = 3000
_SHORT_FORM_DURATION_RATIO = 0.55
_SHORT_FORM_MIN_DURATION_SECONDS = 1200

_PROVIDER_LABELS = {
    "tencent": "腾讯",
    "youku": "优酷",
    "bilibili": "B站",
    "iqiyi": "爱奇艺",
    "mgtv": "芒果",
}


def _compact_title(text: str) -> str:
    return re.sub(r"[\W_《》【】()（）]+", "", normalize_name(text).casefold())


def build_danmaku_series_key(name: str) -> str:
    normalized = normalize_name(strip_episode_suffix(name))
    return _compact_title(normalized)


def _movie_candidate_priority(query_name: str, candidate_name: str) -> tuple[int, int, int]:
    query_base = strip_episode_suffix(normalize_name(query_name))
    candidate_base = strip_episode_suffix(normalize_name(candidate_name))
    candidate_text = normalize_name(candidate_name)
    exact_title = int(_compact_title(candidate_base) == _compact_title(query_base))
    preferred_variant = int(any(token in candidate_text for token in _PREFERRED_MOVIE_VARIANT_TOKENS))
    supplemental = int(any(token in candidate_text for token in _SUPPLEMENTAL_MOVIE_TOKENS))
    return exact_title, preferred_variant, supplemental


def _filter_short_duration_candidates_for_implicit_request(
    items: list[DanmakuSearchItem],
) -> list[DanmakuSearchItem]:
    no_episode_durations = [
        item.duration_seconds for item in items if extract_episode_number(item.name) is None and item.duration_seconds > 0
    ]
    if not no_episode_durations:
        return items
    max_duration = max(no_episode_durations)
    if max_duration < _LONG_FORM_DURATION_SECONDS:
        return items
    min_duration = max(_SHORT_FORM_MIN_DURATION_SECONDS, int(max_duration * _SHORT_FORM_DURATION_RATIO))
    filtered = [
        item
        for item in items
        if item.duration_seconds <= 0 or item.duration_seconds >= min_duration
    ]
    return filtered or items


def _filter_too_short_duration_candidates(items: list[DanmakuSearchItem]) -> list[DanmakuSearchItem]:
    return [
        item
        for item in items
        if item.duration_seconds <= 0 or item.duration_seconds >= _MIN_DANMAKU_CANDIDATE_DURATION_SECONDS
    ]


class DanmakuService:
    def __init__(self, providers: dict[str, DanmakuProvider], provider_order: list[str]) -> None:
        self._providers = dict(providers)
        self._provider_order = list(provider_order)
        self._provider_rank = {key: index for index, key in enumerate(self._provider_order)}

    def _preferred_provider_key(self, reg_src: str) -> str | None:
        matched = match_provider(reg_src)
        if matched and matched in self._providers:
            return matched
        return None

    def _ordered_provider_keys(self, reg_src: str) -> list[str]:
        matched = self._preferred_provider_key(reg_src)
        if matched is not None:
            return [matched]
        return [key for key in self._provider_order if key in self._providers]

    @property
    def provider_order(self) -> list[str]:
        return list(self._provider_order)

    def search_danmu_sources(
        self,
        name: str,
        reg_src: str = "",
        preferred_provider: str = "",
        preferred_page_url: str = "",
        media_duration_seconds: int = 0,
    ) -> DanmakuSourceSearchResult:
        flat_results = self.search_danmu(name, reg_src)
        requested_episode = extract_episode_number(normalize_name(name))
        grouped: dict[str, list[DanmakuSourceOption]] = {}
        for item in flat_results:
            grouped.setdefault(item.provider, []).append(
                DanmakuSourceOption(
                    provider=item.provider,
                    name=item.name,
                    url=item.url,
                    ratio=item.ratio,
                    simi=item.simi,
                    duration_seconds=item.duration_seconds,
                    episode_match=extract_episode_number(item.name) == requested_episode if requested_episode is not None else False,
                    preferred_by_history=item.url == preferred_page_url,
                )
            )
        groups = [
            DanmakuSourceGroup(
                provider=provider,
                provider_label=_PROVIDER_LABELS.get(provider, provider),
                options=options,
                preferred_by_history=provider == preferred_provider,
            )
            for provider, options in grouped.items()
        ]
        return self.rerank_danmaku_source_search_result(
            DanmakuSourceSearchResult(groups=groups),
            reg_src=reg_src,
            preferred_provider=preferred_provider,
            preferred_page_url=preferred_page_url,
            media_duration_seconds=media_duration_seconds,
        )

    def rerank_danmaku_source_search_result(
        self,
        result: DanmakuSourceSearchResult,
        *,
        reg_src: str = "",
        preferred_provider: str = "",
        preferred_page_url: str = "",
        media_duration_seconds: int = 0,
    ) -> DanmakuSourceSearchResult:
        ranked_rows: list[tuple[DanmakuSourceGroup, DanmakuSourceOption, int]] = []
        stable_index = 0
        for group in result.groups:
            for option in group.options:
                ranked_rows.append((group, option, stable_index))
                stable_index += 1
        if media_duration_seconds > 0:
            ranked_rows.sort(
                key=lambda row: self._danmaku_source_option_sort_key(
                    row[1],
                    preferred_provider=preferred_provider,
                    preferred_page_url=preferred_page_url,
                    reg_src=reg_src,
                    media_duration_seconds=media_duration_seconds,
                    stable_index=row[2],
                )
            )
        return self._group_ranked_source_rows(ranked_rows, preferred_provider, preferred_page_url, reg_src)

    def search_danmu(self, name: str, reg_src: str = "") -> list[DanmakuSearchItem]:
        normalized = normalize_name(name)
        search_keyword = strip_episode_suffix(normalized) or normalized
        requested_episode = extract_episode_number(normalized)
        explicit_episode_request = has_explicit_episode_marker(normalized)
        primary_query = search_keyword
        preferred_key = self._preferred_provider_key(reg_src)
        provider_keys = [preferred_key] if preferred_key is not None else self._ordered_provider_keys(reg_src)
        results = self._collect_search_results(provider_keys, primary_query, normalized)
        results = _filter_too_short_duration_candidates(results)
        if requested_episode is not None:
            matching = [
                item
                for item in results
                if extract_episode_number(item.name) == requested_episode
                and episode_title_matches(normalized, item.name)
            ]
            if not matching and preferred_key is not None:
                fallback_keys = [
                    key for key in self._provider_order if key in self._providers and key != preferred_key
                ]
                if fallback_keys:
                    results.extend(self._collect_search_results(fallback_keys, primary_query, normalized))
                    results = _filter_too_short_duration_candidates(results)
                    matching = [
                        item
                        for item in results
                        if extract_episode_number(item.name) == requested_episode
                        and episode_title_matches(normalized, item.name)
                    ]
            no_episode = [item for item in results if extract_episode_number(item.name) is None]
            if matching:
                results = [*matching, *no_episode]
            elif not explicit_episode_request and no_episode:
                results = no_episode
            else:
                results = []
        if requested_episode is not None and not explicit_episode_request:
            results = _filter_short_duration_candidates_for_implicit_request(results)

        def sort_key(item: DanmakuSearchItem) -> tuple[int, int, int, int, int, float, float, int]:
            item_episode = extract_episode_number(item.name)
            no_episode_priority = 0
            episode_priority = 0
            movie_exact_priority = 0
            movie_variant_priority = 0
            supplemental_penalty = 0
            duration_priority = item.duration_seconds
            explicit_episode_priority = 0
            if requested_episode is not None:
                if explicit_episode_request:
                    episode_priority = int(item_episode == requested_episode)
                    explicit_episode_priority = episode_priority
                else:
                    no_episode_priority = int(item_episode is None)
                    episode_priority = int(item_episode == requested_episode)
                    movie_exact_priority, movie_variant_priority, supplemental_penalty = _movie_candidate_priority(
                        primary_query, item.name
                    )
            return (
                -no_episode_priority,
                -movie_exact_priority,
                -movie_variant_priority,
                supplemental_penalty,
                -explicit_episode_priority,
                -duration_priority,
                -episode_priority,
                -item.ratio,
                -item.simi,
                self._provider_rank.get(item.provider, len(self._provider_order)),
            )

        return sorted(
            results,
            key=sort_key,
        )

    def _collect_search_results(
        self, provider_keys: list[str], query_name: str, original_name: str | None = None
    ) -> list[DanmakuSearchItem]:
        results: list[DanmakuSearchItem] = []
        for key in provider_keys:
            try:
                provider_items = self._providers[key].search(query_name, original_name=original_name)
            except Exception as exc:
                logger.warning("Danmaku provider search failed provider=%s name=%s error=%s", key, query_name, exc)
                continue
            for item in provider_items:
                if should_filter_name(query_name, item.name):
                    continue
                ratio = item.ratio or similarity_score(query_name, item.name)
                simi = item.simi or ratio
                results.append(replace(item, ratio=ratio, simi=simi))
        return results

    def _danmaku_source_option_sort_key(
        self,
        option: DanmakuSourceOption,
        *,
        preferred_provider: str,
        preferred_page_url: str,
        reg_src: str,
        media_duration_seconds: int,
        stable_index: int,
    ) -> tuple[int, int, int, int, int, int, int]:
        preferred_page = int(bool(preferred_page_url) and option.url == preferred_page_url)
        preferred_provider_match = int(bool(preferred_provider) and option.provider == preferred_provider)
        reg_src_provider_match = int(option.provider == self._preferred_provider_key(reg_src))
        duration_known = int(option.duration_seconds > 0 and media_duration_seconds > 0)
        duration_gap = abs(option.duration_seconds - media_duration_seconds) if duration_known else 10**9
        return (
            -preferred_page,
            -preferred_provider_match,
            -reg_src_provider_match,
            -int(option.episode_match),
            -duration_known,
            duration_gap,
            stable_index,
        )

    def _group_ranked_source_rows(
        self,
        ranked_rows: list[tuple[DanmakuSourceGroup, DanmakuSourceOption, int]],
        preferred_provider: str,
        preferred_page_url: str,
        reg_src: str,
    ) -> DanmakuSourceSearchResult:
        grouped_options: dict[str, list[DanmakuSourceOption]] = {}
        group_meta: dict[str, DanmakuSourceGroup] = {}
        ordered_providers: list[str] = []
        for source_group, option, _ in ranked_rows:
            provider = source_group.provider
            if provider not in grouped_options:
                grouped_options[provider] = []
                group_meta[provider] = source_group
                ordered_providers.append(provider)
            grouped_options[provider].append(option)
        groups = [
            DanmakuSourceGroup(
                provider=provider,
                provider_label=group_meta[provider].provider_label,
                options=grouped_options[provider],
                preferred_by_history=group_meta[provider].preferred_by_history,
            )
            for provider in ordered_providers
        ]
        default_option = self._pick_default_source_option(groups, preferred_provider, preferred_page_url, reg_src)
        return DanmakuSourceSearchResult(
            groups=groups,
            default_option_url=default_option.url if default_option is not None else "",
            default_provider=default_option.provider if default_option is not None else "",
        )

    def _pick_default_source_option(
        self,
        groups: list[DanmakuSourceGroup],
        preferred_provider: str,
        preferred_page_url: str,
        reg_src: str,
    ) -> DanmakuSourceOption | None:
        for group in groups:
            for option in group.options:
                if preferred_page_url and option.url == preferred_page_url:
                    return option
        if preferred_provider:
            for group in groups:
                if group.provider == preferred_provider and group.options:
                    return group.options[0]
        matched_provider = self._preferred_provider_key(reg_src)
        if matched_provider:
            for group in groups:
                if group.provider == matched_provider and group.options:
                    return group.options[0]
        for group in groups:
            if group.options:
                return group.options[0]
        return None

    def resolve_danmu(self, page_url: str) -> str:
        for key in self._provider_order:
            provider = self._providers.get(key)
            if provider is None or not provider.supports(page_url):
                continue
            records = provider.resolve(page_url)
            if not records:
                raise DanmakuEmptyResultError(f"未找到弹幕: {page_url}")
            return build_xml(records)
        raise ProviderNotSupportedError(f"不支持的弹幕来源: {page_url}")


def create_default_danmaku_service() -> DanmakuService:
    providers = {
        "tencent": TencentDanmakuProvider(),
        "youku": YoukuDanmakuProvider(),
        "bilibili": BilibiliDanmakuProvider(),
        "iqiyi": IqiyiDanmakuProvider(),
        "mgtv": MgtvDanmakuProvider(),
    }
    return DanmakuService(providers, provider_order=["tencent", "youku", "bilibili", "iqiyi", "mgtv"])
