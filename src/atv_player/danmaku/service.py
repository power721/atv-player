from __future__ import annotations

from dataclasses import replace
import logging

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
    extract_episode_number,
    match_provider,
    normalize_name,
    should_filter_name,
    similarity_score,
    strip_episode_suffix,
)


logger = logging.getLogger(__name__)


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
        primary_query = normalized if requested_episode is not None else search_keyword
        preferred_key = self._preferred_provider_key(reg_src)
        provider_keys = [preferred_key] if preferred_key is not None else self._ordered_provider_keys(reg_src)
        results = self._collect_search_results(provider_keys, primary_query)
        if requested_episode is not None:
            matching = [item for item in results if extract_episode_number(item.name) == requested_episode]
            if not matching and preferred_key is not None:
                fallback_keys = [
                    key for key in self._provider_order if key in self._providers and key != preferred_key
                ]
                if fallback_keys:
                    results.extend(self._collect_search_results(fallback_keys, primary_query))
                    matching = [item for item in results if extract_episode_number(item.name) == requested_episode]
            if matching:
                no_episode = [item for item in results if extract_episode_number(item.name) is None]
                results = [*matching, *no_episode]
            else:
                results = []
        return sorted(
            results,
            key=lambda item: (
                -(extract_episode_number(item.name) == requested_episode) if requested_episode is not None else 0,
                -item.ratio,
                -item.simi,
                self._provider_rank.get(item.provider, len(self._provider_order)),
            ),
        )

    def _collect_search_results(self, provider_keys: list[str], query_name: str) -> list[DanmakuSearchItem]:
        results: list[DanmakuSearchItem] = []
        for key in provider_keys:
            try:
                provider_items = self._providers[key].search(query_name)
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
