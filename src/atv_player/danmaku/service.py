from __future__ import annotations

from dataclasses import replace
import logging
import re

from atv_player.danmaku.errors import DanmakuEmptyResultError, ProviderNotSupportedError
from atv_player.danmaku.models import DanmakuSearchItem
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

_LONG_FORM_DURATION_SECONDS = 3000
_SHORT_FORM_DURATION_RATIO = 0.55
_SHORT_FORM_MIN_DURATION_SECONDS = 1200


def _compact_title(text: str) -> str:
    return re.sub(r"[\W_《》【】()（）]+", "", normalize_name(text).casefold())


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

    def search_danmu(self, name: str, reg_src: str = "") -> list[DanmakuSearchItem]:
        normalized = normalize_name(name)
        search_keyword = strip_episode_suffix(normalized) or normalized
        requested_episode = extract_episode_number(normalized)
        explicit_episode_request = has_explicit_episode_marker(normalized)
        primary_query = search_keyword
        preferred_key = self._preferred_provider_key(reg_src)
        provider_keys = [preferred_key] if preferred_key is not None else self._ordered_provider_keys(reg_src)
        results = self._collect_search_results(provider_keys, primary_query, normalized)
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
            if requested_episode is not None:
                if explicit_episode_request:
                    episode_priority = int(item_episode == requested_episode)
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
