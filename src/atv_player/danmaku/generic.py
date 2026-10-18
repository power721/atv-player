from __future__ import annotations

import inspect
import logging
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from atv_player.danmaku.cache import (
    load_cached_danmaku_source_search_result,
    load_cached_danmaku_xml,
    save_cached_danmaku_source_search_result,
    save_cached_danmaku_xml,
)
from atv_player.danmaku.models import (
    DanmakuSeriesPreference,
    DanmakuSourceGroup,
    DanmakuSourceOption,
    DanmakuSourceSearchResult,
)
from atv_player.danmaku.preferences import (
    DanmakuSeriesPreferenceStore,
    _ensure_item_series_key,
    load_item_danmaku_offset,
    save_item_danmaku_offset,
)
from atv_player.danmaku.utils import (
    _play_item_episode_number,
    extract_variety_episode_label,
    has_explicit_episode_marker,
    has_variety_issue_marker,
    infer_playlist_episode_number,
    is_other_fallback_only_result,
    is_stale_other_fallback_result,
    is_variety_collection,
    strip_season_suffix,
)
from atv_player.models import PlayItem

_TITLE_ONLY_ITEM_TITLES = {
    "正片",
    "完整版",
    "全片",
    "电影",
    "播放",
}
_DANMAKU_ENTRY_RE = re.compile(r"<d\b")
logger = logging.getLogger(__name__)


def normalize_danmaku_episode_url(value: str) -> str:
    normalized = str(value or "").strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("请输入完整的 http(s) 单集链接")
    return normalized


def _compose_danmaku_search_query(title: str, episode: str) -> str:
    return " ".join(part for part in (title.strip(), episode.strip()) if part).strip()


def _looks_like_title_only_item(item: PlayItem) -> bool:
    return str(item.title or "").strip() in _TITLE_ONLY_ITEM_TITLES


def _filename_carries_episode_anchor(item: PlayItem, episode_number: int) -> bool:
    """单文件条目的文件名是否自带可信集数锚点（"288 4K.mkv"）。

    集数直接读自文件名而非按播放位置推断时保留锚点；丢锚点会让搜索退化为
    纯标题查询，被同名近似作品夺魁（完美世界 → 优酷"完美的世界"）。四位数字
    （"1917.mkv"）或带年份括注（"300 (2006).mkv"）更可能是纯数字片名的电影，
    不算锚点。
    """
    if _play_item_episode_number(item) is None:
        return False
    if episode_number > 999:
        return False
    return not any(
        value and re.search(r"(?:19|20)\d{2}", str(value))
        for value in (item.original_title, item.title)
    )


def _default_episode_label(item: PlayItem, playlist: list[PlayItem] | None = None) -> str:
    if _looks_like_title_only_item(item):
        return ""
    title = str(item.title or "").strip()
    if not title:
        return ""
    variety_marker_in_filename = has_variety_issue_marker(title)
    variety_collection = is_variety_collection(item.type_name, item.category_name)
    if variety_marker_in_filename or variety_collection:
        variety_label = extract_variety_episode_label(title)
        if variety_label:
            return variety_label
        if variety_marker_in_filename:
            # 文件名只有花絮提示词（纯享/加更/直拍…）而无期号/日期：按播放位置推
            # 集数会指错期，保持标题-only 搜索。
            return ""
        # 分类为综艺但集名是常规命名（"第N集"、数字集名）：回退到常规集数解析，
        # 保证弹幕搜索带上集数锚点。
    episode_number = infer_playlist_episode_number(item, playlist)
    if episode_number is None:
        return ""
    if title.isdigit():
        return str(episode_number)
    if has_explicit_episode_marker(item.title) or len(playlist or []) != 1:
        return f"{episode_number}集"
    if _filename_carries_episode_anchor(item, episode_number):
        return f"{episode_number}集"
    return ""


def _find_selected_option(item: PlayItem, page_url: str) -> DanmakuSourceOption | None:
    for group in item.danmaku_candidates:
        for option in group.options:
            if option.url == page_url:
                return option
    return None


class GenericDanmakuController:
    def __init__(
        self,
        danmaku_service: Any,
        danmaku_preference_store: DanmakuSeriesPreferenceStore | None = None,
    ) -> None:
        self._danmaku_service = danmaku_service
        self._danmaku_preference_store = danmaku_preference_store
        self._danmaku_log_handler: Callable[[str], None] | None = None

    def load_danmaku_offset(
        self,
        item: PlayItem,
        playlist: list[PlayItem] | None = None,
    ) -> float:
        value = load_item_danmaku_offset(
            self._danmaku_preference_store,
            item,
            playlist,
        )
        item.danmaku_offset_seconds = value
        return value

    def save_danmaku_offset(
        self,
        item: PlayItem,
        value: float,
        playlist: list[PlayItem] | None = None,
    ) -> None:
        item.danmaku_offset_seconds = save_item_danmaku_offset(
            self._danmaku_preference_store,
            item,
            value,
            playlist,
        )

    def set_danmaku_log_handler(self, handler: Callable[[str], None] | None) -> None:
        self._danmaku_log_handler = handler

    def _source_preference(self, item: PlayItem) -> DanmakuSeriesPreference | None:
        if self._danmaku_preference_store is None:
            return None
        series_key = _ensure_item_series_key(item)
        if not series_key:
            return None
        return self._danmaku_preference_store.load(series_key)

    def _save_source_preference(self, item: PlayItem) -> None:
        if self._danmaku_preference_store is None:
            return
        series_key = _ensure_item_series_key(item)
        if not series_key:
            return
        existing = self._danmaku_preference_store.load(series_key)

        def _or_existing(current: str, attr: str) -> str:
            return current or (getattr(existing, attr) if existing is not None else "")

        self._danmaku_preference_store.save(
            DanmakuSeriesPreference(
                series_key=series_key,
                provider=_or_existing(item.selected_danmaku_provider, "provider"),
                page_url=_or_existing(item.selected_danmaku_url, "page_url"),
                title=_or_existing(item.selected_danmaku_title, "title"),
                search_title=_or_existing(item.danmaku_search_title, "search_title"),
                updated_at=int(time.time()),
                episode_source_offsets=existing.episode_source_offsets if existing is not None else {},
            )
        )

    def _log_danmaku_event(self, event: str, item: PlayItem | None = None, detail: str = "") -> None:
        handler = self._danmaku_log_handler
        if handler is None:
            return
        label = ""
        if item is not None:
            label = (item.title or item.media_title or "").strip()
        parts = [event]
        if label:
            parts.append(label)
        if detail:
            parts.append(detail)
        message = ": ".join(parts) if len(parts) > 1 else parts[0]
        try:
            handler(message)
        except Exception:
            logger.exception("Danmaku log handler failed event=%s", event)

    def _search_title(self, item: PlayItem) -> str:
        return item.danmaku_search_title.strip() or item.media_title.strip() or item.title.strip()

    def _search_episode(self, item: PlayItem, playlist: list[PlayItem] | None = None) -> str:
        return item.danmaku_search_episode.strip() or _default_episode_label(item, playlist).strip()

    def resolve_default_danmaku_search_episode(
        self,
        item: PlayItem,
        playlist: list[PlayItem] | None = None,
    ) -> str:
        """Dialog-facing recompute of the default episode label (see plugin controller)."""
        return _default_episode_label(item, playlist).strip()

    def _search_query(self, item: PlayItem, playlist: list[PlayItem] | None = None) -> str:
        if item.danmaku_search_query_overridden and item.danmaku_search_query.strip():
            return item.danmaku_search_query.strip()
        title = self._search_title(item)
        episode = self._search_episode(item, playlist)
        item.danmaku_search_title = title
        item.danmaku_search_episode = episode
        item.danmaku_search_query = _compose_danmaku_search_query(title, episode)
        return item.danmaku_search_query

    def _count_danmaku_entries(self, xml_text: str) -> int:
        return len(_DANMAKU_ENTRY_RE.findall(str(xml_text or "")))

    def _reg_src(self, item: PlayItem) -> str:
        return str(item.vod_id or item.url or "").strip()

    def _save_source_search_result(self, query_name: str, reg_src: str, result: DanmakuSourceSearchResult) -> None:
        save_cached_danmaku_source_search_result(query_name, reg_src, result)
        if reg_src:
            save_cached_danmaku_source_search_result(query_name, "", result)

    def _load_source_search_result(self, query_name: str, reg_src: str) -> DanmakuSourceSearchResult | None:
        cached = load_cached_danmaku_source_search_result(query_name, reg_src)
        if cached is None and reg_src:
            cached = load_cached_danmaku_source_search_result(query_name, "")
        if cached is not None and is_stale_other_fallback_result(cached, reg_src):
            # 历史污染条目：other 兜底候选指向别的 reg_src，直接弃用，走新搜索。
            return None
        return cached

    def _apply_source_search_result(self, item: PlayItem, result: DanmakuSourceSearchResult) -> None:
        item.danmaku_candidates = result.groups
        item.selected_danmaku_provider = result.default_provider
        item.selected_danmaku_url = result.default_option_url
        item.selected_danmaku_title = ""
        for group in result.groups:
            for option in group.options:
                if option.url == result.default_option_url:
                    item.selected_danmaku_title = option.name
                    break
            if item.selected_danmaku_title:
                break
        item.danmaku_error = ""

    def _rerank_source_search_result(
        self,
        result: DanmakuSourceSearchResult,
        *,
        query_name: str,
        reg_src: str,
        media_duration_seconds: int = 0,
        preferred_provider: str = "",
        preferred_page_url: str = "",
    ) -> DanmakuSourceSearchResult:
        rerank = getattr(self._danmaku_service, "rerank_danmaku_source_search_result", None)
        if not callable(rerank):
            return result
        kwargs: dict[str, Any] = {
            "query_name": query_name,
            "reg_src": reg_src,
            "media_duration_seconds": media_duration_seconds,
        }
        params = inspect.signature(rerank).parameters
        if "preferred_provider" in params:
            kwargs["preferred_provider"] = preferred_provider
        if "preferred_page_url" in params:
            kwargs["preferred_page_url"] = preferred_page_url
        return rerank(result, **kwargs)

    def _fallback_search_queries(
        self,
        item: PlayItem,
        primary_query: str,
        provider_filter: str = "",
    ) -> list[str]:
        """0 结果后的降级查询序列:季名剥除标题(+集数)→ 剥除标题 → 纯标题。

        网盘资源名自带的"年番N/第N季"后缀在弹幕库往往没有同名条目,整季 0
        结果;先试剥掉季名并保留集数锚点,再退到纯标题。按源过滤(用户定向
        搜索)或手动指定过与自动组合不同的查询时不降级,尊重显式意图。
        """
        if provider_filter:
            return []
        title = item.danmaku_search_title.strip()
        episode = item.danmaku_search_episode.strip()
        if not title:
            return []
        if item.danmaku_search_query_overridden and item.danmaku_search_query.strip() != _compose_danmaku_search_query(
            title, episode
        ):
            return []
        stripped_title = strip_season_suffix(title)
        candidates: list[str] = []
        if stripped_title and stripped_title != title:
            if episode:
                candidates.append(_compose_danmaku_search_query(stripped_title, episode))
            candidates.append(stripped_title)
        if episode:
            candidates.append(title)
        queries: list[str] = []
        for candidate in candidates:
            if candidate and candidate != primary_query and candidate not in queries:
                queries.append(candidate)
        return queries

    def _search_sources(
        self,
        query_name: str,
        reg_src: str,
        *,
        media_duration_seconds: int = 0,
        provider_filter: str = "",
        preferred_provider: str = "",
        preferred_page_url: str = "",
    ) -> DanmakuSourceSearchResult:
        search_sources = getattr(self._danmaku_service, "search_danmu_sources", None)
        if callable(search_sources):
            kwargs: dict[str, Any] = {"media_duration_seconds": media_duration_seconds}
            params = inspect.signature(search_sources).parameters
            if "provider_filter" in params:
                kwargs["provider_filter"] = provider_filter
            if "preferred_provider" in params:
                kwargs["preferred_provider"] = preferred_provider
            if "preferred_page_url" in params:
                kwargs["preferred_page_url"] = preferred_page_url
            return search_sources(query_name, reg_src, **kwargs)
        candidates = self._danmaku_service.search_danmu(query_name, reg_src, provider_filter=provider_filter)
        grouped: dict[str, list[DanmakuSourceOption]] = {}
        for item in candidates:
            grouped.setdefault(item.provider, []).append(
                DanmakuSourceOption(
                    provider=item.provider,
                    name=item.name,
                    url=item.url,
                    ratio=getattr(item, "ratio", 0.0),
                    simi=getattr(item, "simi", 0.0),
                    duration_seconds=getattr(item, "duration_seconds", 0),
                    resolve_context=dict(getattr(item, "resolve_context", {})),
                )
            )
        groups = [
            DanmakuSourceGroup(provider=provider, provider_label=provider, options=options)
            for provider, options in grouped.items()
        ]
        default_option = groups[0].options[0] if groups and groups[0].options else None
        return DanmakuSourceSearchResult(
            groups=groups,
            default_option_url="" if default_option is None else default_option.url,
            default_provider="" if default_option is None else default_option.provider,
        )

    def load_cached_danmaku_sources(
        self,
        item: PlayItem,
        playlist: list[PlayItem] | None = None,
        media_duration_seconds: int = 0,
    ) -> bool:
        query_name = self._search_query(item, playlist)
        reg_src = self._reg_src(item)
        if not query_name:
            return False
        cached = self._load_source_search_result(query_name, reg_src)
        if cached is None:
            return False
        preference = self._source_preference(item)
        preferred_provider = preference.provider if preference is not None else ""
        preferred_page_url = preference.page_url if preference is not None else ""
        self._apply_source_search_result(
            item,
            self._rerank_source_search_result(
                cached,
                query_name=query_name,
                reg_src=reg_src,
                media_duration_seconds=media_duration_seconds,
                preferred_provider=preferred_provider,
                preferred_page_url=preferred_page_url,
            ),
        )
        return True

    def refresh_danmaku_sources(
        self,
        item: PlayItem,
        query_override: str | None = None,
        search_title_override: str | None = None,
        search_episode_override: str | None = None,
        playlist: list[PlayItem] | None = None,
        force_refresh: bool = False,
        media_duration_seconds: int = 0,
        provider_filter: str = "",
    ) -> None:
        if search_title_override is not None:
            item.danmaku_search_title = search_title_override.strip()
        if search_episode_override is not None:
            item.danmaku_search_episode = search_episode_override.strip()
        if query_override is not None:
            item.danmaku_search_query = query_override.strip()
        item.danmaku_search_provider = provider_filter
        item.danmaku_search_query_overridden = (
            query_override is not None or search_title_override is not None or search_episode_override is not None
        )
        query_name = self._search_query(item, playlist)
        reg_src = self._reg_src(item)
        if not query_name:
            return
        self._log_danmaku_event("弹幕搜索中", detail=query_name)
        if not force_refresh and not provider_filter and self.load_cached_danmaku_sources(
            item,
            playlist=playlist,
            media_duration_seconds=media_duration_seconds,
        ):
            candidate_count = sum(len(group.options) for group in item.danmaku_candidates)
            self._log_danmaku_event("弹幕搜索成功", detail=f"找到 {candidate_count} 个候选")
            return
        preference = self._source_preference(item)
        preferred_provider = preference.provider if preference is not None else ""
        preferred_page_url = preference.page_url if preference is not None else ""
        result = self._search_sources(
            query_name,
            reg_src,
            media_duration_seconds=media_duration_seconds,
            provider_filter=provider_filter,
            preferred_provider=preferred_provider,
            preferred_page_url=preferred_page_url,
        )
        for fallback_query in self._fallback_search_queries(item, query_name, provider_filter):
            if result.groups:
                break
            self._log_danmaku_event("弹幕搜索中", detail=fallback_query)
            fallback_result = self._rerank_source_search_result(
                self._search_sources(
                    fallback_query,
                    reg_src,
                    media_duration_seconds=media_duration_seconds,
                    provider_filter=provider_filter,
                    preferred_provider=preferred_provider,
                    preferred_page_url=preferred_page_url,
                ),
                query_name=query_name,
                reg_src=reg_src,
                media_duration_seconds=media_duration_seconds,
                preferred_provider=preferred_provider,
                preferred_page_url=preferred_page_url,
            )
            if fallback_result.groups:
                result = fallback_result
        if not provider_filter and not is_other_fallback_only_result(result):
            self._save_source_search_result(query_name, reg_src, result)
        self._apply_source_search_result(item, result)
        candidate_count = sum(len(group.options) for group in item.danmaku_candidates)
        self._log_danmaku_event("弹幕搜索成功", detail=f"找到 {candidate_count} 个候选")

    def auto_resolve_danmaku(
        self,
        item: PlayItem,
        playlist: list[PlayItem] | None = None,
        *,
        media_duration_seconds: int = 0,
    ) -> bool:
        """Find and download the default danmaku source for one playlist item."""
        if item.danmaku_xml:
            return True
        query_override = (
            item.danmaku_search_query
            if item.danmaku_search_query_overridden
            else None
        )
        self.refresh_danmaku_sources(
            item,
            query_override=query_override,
            playlist=playlist,
            media_duration_seconds=media_duration_seconds,
        )
        selected_url = str(item.selected_danmaku_url or "").strip()
        if not selected_url:
            return False
        self.switch_danmaku_source(item, selected_url)
        return bool(item.danmaku_xml)

    def prefetch_next_episode_danmaku(
        self,
        item: PlayItem,
        playlist: list[PlayItem],
    ) -> None:
        if (
            item.danmaku_xml
            or item.danmaku_pending
            or not self._search_episode(item, playlist)
        ):
            return
        prefetch_label = self._search_query(item, playlist)
        if not prefetch_label:
            return
        self._log_danmaku_event("弹幕预下载中", detail=prefetch_label)
        if self.auto_resolve_danmaku(item, playlist):
            self._log_danmaku_event(
                "弹幕预下载成功",
                detail=f"{self._count_danmaku_entries(item.danmaku_xml)} 条弹幕",
            )

    def switch_danmaku_source(self, item: PlayItem, page_url: str) -> str:
        selected_option = _find_selected_option(item, page_url)
        selected_provider = ""
        if selected_option is not None:
            for group in item.danmaku_candidates:
                if any(option.url == page_url for option in group.options):
                    selected_provider = str(group.provider_label or "").strip()
                    break
        option_label = (selected_option.name if selected_option is not None else "").strip()
        download_detail = option_label
        if selected_provider and option_label:
            download_detail = f"{selected_provider} - {option_label}"
        elif selected_provider:
            download_detail = selected_provider
        self._log_danmaku_event("弹幕下载中", detail=download_detail)
        query_name = item.danmaku_search_query.strip() or self._search_query(item)
        reg_src = self._reg_src(item)
        cached_xml = load_cached_danmaku_xml(query_name, page_url)
        if cached_xml:
            xml_text = cached_xml
        else:
            resolve = self._danmaku_service.resolve_danmu
            if selected_option is not None and "option" in inspect.signature(resolve).parameters:
                xml_text = resolve(page_url, option=selected_option)
            else:
                xml_text = resolve(page_url)
            save_cached_danmaku_xml(query_name, page_url, xml_text)
        if reg_src:
            save_cached_danmaku_xml(query_name, reg_src, xml_text)
        item.danmaku_xml = xml_text
        item.selected_danmaku_url = page_url
        if selected_option is not None:
            item.selected_danmaku_provider = selected_option.provider
            item.selected_danmaku_title = selected_option.name
        item.danmaku_error = ""
        self._save_source_preference(item)
        self._log_danmaku_event("弹幕下载成功", detail=f"{self._count_danmaku_entries(xml_text)} 条弹幕")
        return xml_text

    def download_danmaku_from_url(self, item: PlayItem, page_url: str) -> str:
        normalized_url = normalize_danmaku_episode_url(page_url)
        provider_key = self._danmaku_service.provider_key_for_url(normalized_url)
        source_title = (item.title or item.media_title or "单集弹幕").strip()
        self._log_danmaku_event("弹幕下载中", detail=f"{provider_key} - {source_title}")
        query_name = item.danmaku_search_query.strip() or self._search_query(item)
        reg_src = self._reg_src(item)
        xml_text = load_cached_danmaku_xml(query_name, normalized_url)
        if not xml_text:
            xml_text = self._danmaku_service.resolve_danmu(normalized_url)
            save_cached_danmaku_xml(query_name, normalized_url, xml_text)
        if reg_src:
            save_cached_danmaku_xml(query_name, reg_src, xml_text)
        item.danmaku_xml = xml_text
        item.selected_danmaku_provider = provider_key
        item.selected_danmaku_url = normalized_url
        item.selected_danmaku_title = source_title
        item.danmaku_error = ""
        self._save_source_preference(item)
        self._log_danmaku_event(
            "弹幕下载成功",
            detail=f"{self._count_danmaku_entries(xml_text)} 条弹幕",
        )
        return xml_text
