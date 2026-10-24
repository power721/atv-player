from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import inspect
import json
import logging
import re
import shutil
import threading
import time
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from atv_player.api import ApiError
from atv_player.karaoke import parse_raw_karaoke, render_karaoke_ass
from atv_player.controllers.player_controller import PlayerSession
from atv_player.danmaku.cache import (
    load_cached_danmaku_source_search_result,
    load_cached_danmaku_xml,
    save_cached_danmaku_source_search_result,
    save_cached_danmaku_xml,
)
from atv_player.danmaku.generic import normalize_danmaku_episode_url
from atv_player.danmaku.models import DanmakuSeriesPreference, DanmakuSourceGroup, DanmakuSourceOption
from atv_player.danmaku.preferences import (
    load_item_danmaku_offset,
    save_item_danmaku_offset,
)
from atv_player.danmaku.service import build_danmaku_series_key
from atv_player.danmaku.utils import (
    episode_title_matches,
    extract_episode_number,
    extract_official_link_url,
    extract_part_number,
    extract_variety_episode_label,
    has_explicit_episode_marker,
    has_variety_issue_marker,
    infer_playlist_episode_number,
    is_other_fallback_only_result,
    is_stale_other_fallback_result,
    is_variety_collection,
    match_provider,
    normalize_name,
    strip_episode_suffix,
)
from atv_player.drive_links import DRIVE_SHARE_DOMAINS, looks_like_drive_share_link
from atv_player.controllers.browse_controller import _map_vod_item, build_drive_grouped_sources
from atv_player.controllers.douban_controller import _map_item
from atv_player.controllers.douban_controller import _coerce_category_id
from atv_player.controllers.pagination import page_count_from_payload
from atv_player.controllers.telegram_search_controller import build_detail_playlist
from atv_player.episode_titles import seed_original_titles
from atv_player.episode_titles import extract_season_number
from atv_player.metadata.query import is_short_drama_collection
from atv_player.models import (
    CategoryFilter,
    CategoryFilterOption,
    DoubanCategory,
    ExternalSubtitleOption,
    OpenPlayerRequest,
    PlaybackSource,
    PlaybackSourceGroup,
    PlayItem,
    PlaybackDetailAction,
    PlaybackDetailFieldAction,
    PlaybackDetailField,
    PlaybackDetailValuePart,
    PlaybackLoadResult,
    SpiderDanmakuDirectSource,
    VideoQualityOption,
    SpiderPluginAction,
    SpiderPluginRawCategory,
    VodItem,
)
from atv_player.paths import app_cache_dir
from atv_player.playback_parsers import coerce_media_url
from atv_player.player.resume import resolve_resume_index
from atv_player.plugins.category_overrides import apply_category_overrides, parse_category_overrides_json


logger = logging.getLogger(__name__)
_GROUPED_ROUTE_RE = re.compile(r"^(?P<group>\S*?\D)(?P<number>\d+)$")
_MOVIE_LIKE_TITLE_MARKERS = ("剧场版",)
_MOVIE_CATEGORY_MARKERS = ("电影",)
_SINGLE_VIDEO_GENERIC_TITLES = {"正片", "完整版", "全片"}


@dataclass
class _InflightDanmakuSourceSearch:
    event: threading.Event
    result: object | None = None
    error: Exception | None = None


def _map_spider_page_count(
    payload: Mapping[str, object],
    items: list[VodItem],
) -> int:
    return page_count_from_payload(payload, fallback_total=len(items))


def _format_spider_method_call_message(plugin_name: str, method_name: str, **params: object) -> str:
    args = " ".join(f"{key}={value!r}" for key, value in params.items())
    return f"Spider plugin call plugin={plugin_name} method={method_name} {args}".rstrip()


def _strip_trailing_title_size_suffix(value: str) -> str:
    title = str(value or "").strip()
    if not title:
        return ""
    stripped = re.sub(
        r"\s*[\(（\[【]\s*\d+(?:\.\d+)?\s*(?:kb|mb|gb|tb)\s*[\)）\]】]\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    stripped = stripped.strip()
    return stripped or title


def _strip_trailing_title_year_suffix(value: str) -> str:
    title = str(value or "").strip()
    if not title:
        return ""
    stripped = re.sub(r"\s*[\(（\[【]\s*(?:19|20)\d{2}\s*[\)）\]】]\s*$", "", title)
    stripped = stripped.strip()
    return stripped or title


def _normalize_default_danmaku_search_title(value: str) -> str:
    return _strip_trailing_title_year_suffix(_strip_trailing_title_size_suffix(value))


def _should_prefer_default_danmaku_search_title(default_title: str, preferred_title: str) -> bool:
    normalized_default = _normalize_default_danmaku_search_title(default_title)
    normalized_preferred = _normalize_default_danmaku_search_title(preferred_title)
    if not normalized_default or not normalized_preferred:
        return False
    default_season = extract_season_number(normalized_default)
    preferred_season = extract_season_number(normalized_preferred)
    if default_season is None or preferred_season is not None:
        return False
    default_base = re.sub(
        r"(?:\s*[-:：]\s*)?(?:第\s*[0-9零一二两三四五六七八九十百千]+\s*季|season\s*\d+|s\d+)\s*$",
        "",
        normalize_name(normalized_default),
        flags=re.IGNORECASE,
    ).strip()
    preferred_base = normalize_name(normalized_preferred)
    return bool(default_base and default_base == preferred_base)


def _looks_like_offline_download_link(value: str) -> bool:
    candidate = value.strip().lower()
    return candidate.startswith("magnet:?") or candidate.startswith("ed2k://")


def _map_filter_option(payload: object) -> CategoryFilterOption | None:
    if not isinstance(payload, dict):
        return None
    name = str(payload.get("n") or "").strip()
    value = str(payload.get("v") or "").strip()
    if not name:
        return None
    return CategoryFilterOption(name=name, value=value)


def _map_category_filters(payload: object) -> list[CategoryFilter]:
    if not isinstance(payload, list):
        return []
    groups: list[CategoryFilter] = []
    for raw_group in payload:
        if not isinstance(raw_group, dict):
            continue
        key = str(raw_group.get("key") or "").strip()
        name = str(raw_group.get("name") or "").strip()
        if not key or not name:
            continue
        options = [
            option
            for option in (_map_filter_option(raw_option) for raw_option in raw_group.get("value") or [])
            if option is not None
        ]
        if not options:
            continue
        groups.append(CategoryFilter(key=key, name=name, options=options))
    return groups


def _looks_like_media_url(value: str) -> bool:
    candidate = value.strip().lower()
    if candidate.endswith(".html"):
        return False
    if candidate.startswith(("http://", "https://", "rtmp://", "rtsp://")):
        return True
    return any(candidate.endswith(ext) or f"{ext}?" in candidate for ext in (".m3u8", ".mkv", ".mp4", ".flv"))


def _has_implicit_numeric_title(value: str) -> bool:
    return re.fullmatch(r"\s*0*\d{1,4}\s*", value or "") is not None


def _looks_like_calendar_episode_title(value: str) -> bool:
    candidate = (value or "").strip()
    if not candidate:
        return False
    return (
        re.match(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:\D|$)", candidate) is not None
        or re.match(r"^(?:19|20)\d{2}[-/.年](?:0?[1-9]|1[0-2])[-/.月](?:0?[1-9]|[12]\d|3[01])(?:日|\D|$)", candidate)
        is not None
    )


def _is_short_bare_numeric_playlist(item: PlayItem, playlist: list[PlayItem] | None = None) -> bool:
    if item.danmaku_title_only:
        return True
    if not _has_implicit_numeric_title(item.title) or not playlist:
        return False
    if len(playlist) < 2 or len(playlist) > 4:
        return False
    return all(_has_implicit_numeric_title(candidate.title) for candidate in playlist)


def _looks_like_movie_title(item: PlayItem) -> bool:
    return any(
        marker in normalize_name(value)
        for value in (item.title, item.media_title)
        for marker in _MOVIE_LIKE_TITLE_MARKERS
        if value
    )


def _looks_like_movie_category(item: PlayItem) -> bool:
    normalized_category_name = normalize_name(item.category_name)
    return any(marker in normalized_category_name for marker in _MOVIE_CATEGORY_MARKERS)


def _should_omit_default_episode_label(item: PlayItem, playlist: list[PlayItem] | None = None) -> bool:
    del playlist
    normalized_title = normalize_name(item.title)
    if normalized_title in _SINGLE_VIDEO_GENERIC_TITLES:
        return True
    if extract_part_number(normalized_title) is not None:
        # "上集/中部/（下）"这类分部标记本身就是集数锚点:电影分上中下的场景
        # 若因电影分类丢掉锚点,三个分段的弹幕就无从区分了。注意只认分部标记,
        # "第1集"这类数字集标仍按电影分类省略(单条目电影带集标会错配剧集)。
        return False
    if _looks_like_movie_category(item):
        return True
    if _looks_like_movie_title(item):
        return True
    return False


def _extract_episode_label(item: PlayItem, playlist: list[PlayItem] | None = None) -> str:
    variety_marker_in_filename = has_variety_issue_marker(item.title)
    if variety_marker_in_filename or is_variety_collection(item.type_name, item.category_name):
        variety_label = extract_variety_episode_label(item.title)
        if variety_label:
            return variety_label
        if variety_marker_in_filename:
            # 文件名只有花絮提示词（纯享/加更/直拍…）而无期号/日期：按播放位置推集数
            # 会指错期，保持标题-only 搜索。
            return ""
        # 分类为综艺但集名是常规命名（"第N集"、数字集名）：回退到常规集数解析，
        # 否则集数为空会让弹幕搜索退化为纯标题查询，候选被时长过滤清空。
    if _looks_like_calendar_episode_title(item.title):
        return _strip_trailing_title_size_suffix(item.title)
    if _should_omit_default_episode_label(item, playlist):
        return ""
    episode_number = infer_playlist_episode_number(item, playlist)
    if episode_number is None:
        return ""
    if _is_short_bare_numeric_playlist(item, playlist):
        return ""
    if _has_implicit_numeric_title(item.title):
        return str(episode_number)
    return f"{episode_number}集"


def _mark_short_bare_numeric_playlist(playlist: list[PlayItem]) -> list[PlayItem]:
    if len(playlist) < 2 or len(playlist) > 4:
        return playlist
    if not all(_has_implicit_numeric_title(item.title) for item in playlist):
        return playlist
    for item in playlist:
        item.danmaku_title_only = True
    return playlist


def _build_danmaku_search_name(item: PlayItem, playlist: list[PlayItem] | None = None) -> str:
    media_title = item.media_title.strip()
    if not media_title:
        return item.title.strip()
    episode_label = _extract_episode_label(item, playlist)
    return " ".join(part for part in (media_title, episode_label) if part).strip()


def _compose_danmaku_search_query(title: str, episode: str) -> str:
    return " ".join(part for part in (title.strip(), episode.strip()) if part).strip()


def _should_retry_danmaku_search_title_only(item: PlayItem) -> bool:
    search_title = item.danmaku_search_title.strip()
    search_episode = item.danmaku_search_episode.strip()
    if not search_episode:
        return False
    if item.danmaku_search_query_overridden:
        query_name = item.danmaku_search_query.strip()
        if not query_name or query_name != _compose_danmaku_search_query(search_title, search_episode):
            return False
    if has_explicit_episode_marker(item.title):
        return False
    if _looks_like_calendar_episode_title(item.title):
        return False
    return True


def _save_cached_danmaku_source_search_result_variants(
    name: str,
    reg_src: str,
    result,
) -> None:
    save_cached_danmaku_source_search_result(name, reg_src, result)
    if reg_src:
        save_cached_danmaku_source_search_result(name, "", result)


def _load_cached_danmaku_source_search_result_variants(name: str, reg_src: str):
    cached = load_cached_danmaku_source_search_result(name, reg_src)
    if cached is not None or not reg_src:
        return cached
    return load_cached_danmaku_source_search_result(name, "")


def _danmaku_cache_query_names(
    item: PlayItem,
    query_name: str,
    playlist: list[PlayItem] | None = None,
) -> list[str]:
    names: list[str] = []
    for candidate in (
        query_name,
        _build_danmaku_search_name(item, playlist),
        str(item.danmaku_search_title or "").strip(),
    ):
        normalized = str(candidate).strip()
        if normalized and normalized not in names:
            names.append(normalized)
    return names


def _should_prefetch_danmaku(item: PlayItem, playlist: list[PlayItem] | None = None) -> bool:
    if _is_short_drama_item(item):
        return False
    return bool(_extract_episode_label(item, playlist))


def _should_auto_resolve_danmaku(item: PlayItem, playlist: list[PlayItem] | None = None) -> bool:
    if _is_short_drama_item(item):
        return False
    if _should_prefetch_danmaku(item, playlist):
        return True
    return _looks_like_movie_category(item) or _looks_like_movie_title(item)


def _is_short_drama_item(item: PlayItem) -> bool:
    return is_short_drama_collection(
        item.media_title or item.title,
        item.category_name,
        item.type_name,
    )


def _is_short_drama_vod(vod: VodItem) -> bool:
    return is_short_drama_collection(
        vod.vod_name,
        vod.category_name,
        vod.type_name,
    )


def _count_danmaku_entries(xml_text: str) -> int:
    if not xml_text:
        return 0
    return len(re.findall(r"<d\b", xml_text))


_SPIDER_DANMAKU_MAX_XML_BYTES = 8 * 1024 * 1024
_SPIDER_DANMAKU_FETCH_TIMEOUT_SECONDS = 10.0
_SPIDER_DANMAKU_HEADER_WHITELIST = {"referer", "user-agent", "cookie"}


def _spider_danmaku_xml_valid(xml_text: str) -> bool:
    if not xml_text or "<d" not in xml_text:
        return False
    return len(xml_text.encode("utf-8", errors="ignore")) <= _SPIDER_DANMAKU_MAX_XML_BYTES


def _map_spider_danmaku_source(payload: object) -> SpiderDanmakuDirectSource | None:
    """解析 playerContent payload 的 `danmaku` 字段(直供弹幕声明)。

    值域:URL 字符串 / 内联 XML 字符串 / {"url":..., "header":{...}} / {"xml":...} /
    [{"name":..., "url":...}] 列表(TVBox 弹幕约定,取首个带 URL 条目)。
    旧 TVBox 布尔 `danmu` 不是本字段,继续被忽略。
    """
    if isinstance(payload, Mapping):
        inline_xml = str(payload.get("xml") or "").strip()
        if _spider_danmaku_xml_valid(inline_xml):
            return SpiderDanmakuDirectSource(xml=inline_xml)
        url = str(payload.get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            return SpiderDanmakuDirectSource(url=url, headers=_normalize_headers(payload.get("header")))
        _log_invalid_spider_danmaku_payload(payload)
        return None
    if isinstance(payload, str):
        value = payload.strip()
        if _spider_danmaku_xml_valid(value):
            return SpiderDanmakuDirectSource(xml=value)
        if value.startswith(("http://", "https://")):
            return SpiderDanmakuDirectSource(url=value)
        _log_invalid_spider_danmaku_payload(payload)
        return None
    if isinstance(payload, list):
        for entry in payload:
            if not isinstance(entry, Mapping):
                continue
            url = str(entry.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                headers = _normalize_headers(entry.get("header"))
                return SpiderDanmakuDirectSource(url=url, headers=headers)
        _log_invalid_spider_danmaku_payload(payload)
        return None
    return None


def _log_invalid_spider_danmaku_payload(payload: object) -> None:
    if payload in (None, "", {}, []):
        return
    logger.warning(
        "Spider plugin playerContent returned unsupported danmaku payload type=%s",
        type(payload).__name__,
    )


def _query_requests_explicit_episode(query_name: str) -> tuple[bool, int | None]:
    normalized_query = normalize_name(query_name)
    if not normalized_query or not has_explicit_episode_marker(normalized_query):
        return False, None
    return True, extract_episode_number(normalized_query)


def _prioritize_requested_episode_in_source_search_result(query_name: str, result):
    normalized_query = normalize_name(query_name)
    if not normalized_query or not has_explicit_episode_marker(normalized_query):
        return result
    requested_episode = extract_episode_number(normalized_query)
    if requested_episode is None:
        return result
    reordered_groups: list[DanmakuSourceGroup] = []
    matched_default: DanmakuSourceOption | None = None
    matched_any = False
    for group in result.groups:
        matching = [
            option
            for option in group.options
            if extract_episode_number(option.name) == requested_episode
            and episode_title_matches(normalized_query, option.name)
        ]
        if not matching:
            reordered_groups.append(group)
            continue
        matched_any = True
        reordered = [*matching, *[option for option in group.options if option not in matching]]
        reordered_groups.append(replace(group, options=reordered))
        if matched_default is None:
            matched_default = reordered[0]
    if not matched_any:
        return result
    return replace(
        result,
        groups=reordered_groups,
        default_option_url=matched_default.url if matched_default is not None else result.default_option_url,
        default_provider=matched_default.provider if matched_default is not None else result.default_provider,
    )


def _compact_danmaku_title(value: str) -> str:
    return re.sub(r"[\W_《》【】()（）]+", "", normalize_name(value).casefold())


def _cached_danmaku_source_result_matches_query(query_name: str, result) -> bool:
    normalized_query = normalize_name(query_name)
    if not normalized_query:
        return True
    if not getattr(result, "groups", None):
        return False
    if not has_explicit_episode_marker(normalized_query):
        return True
    requested_episode = extract_episode_number(normalized_query)
    if requested_episode is None:
        return True
    query_title_key = _compact_danmaku_title(strip_episode_suffix(normalized_query))
    has_related_candidate = False
    for group in result.groups:
        for option in group.options:
            candidate_title_key = _compact_danmaku_title(strip_episode_suffix(option.name))
            if query_title_key and (not candidate_title_key or query_title_key not in candidate_title_key):
                continue
            has_related_candidate = True
            if (
                extract_episode_number(option.name) == requested_episode
                and episode_title_matches(normalized_query, option.name)
            ):
                return True
    return not has_related_candidate


def _cached_danmaku_title_matches_query(query_name: str, candidate_title: str) -> bool:
    explicit_episode_requested, requested_episode = _query_requests_explicit_episode(query_name)
    if not explicit_episode_requested or requested_episode is None:
        return True
    return (
        extract_episode_number(candidate_title) == requested_episode
        and episode_title_matches(query_name, candidate_title)
    )


def _load_series_level_cached_danmaku_source_result(
    item: PlayItem,
    reg_src: str,
):
    search_title = str(item.danmaku_search_title or "").strip()
    if not search_title:
        return None
    return _load_cached_danmaku_source_search_result_variants(search_title, reg_src)


def _build_danmaku_success_detail(
    item: PlayItem,
    danmaku_count: int,
    playlist: list[PlayItem] | None = None,
) -> str:
    label = (
        str(item.danmaku_search_episode or "").strip()
        or _extract_episode_label(item, playlist).strip()
    )
    if not label:
        selected_title = str(item.selected_danmaku_title or "").strip()
        selected_episode = extract_episode_number(selected_title)
        if selected_episode is not None:
            label = f"{selected_episode}集"
    count_label = f"{danmaku_count} 条弹幕"
    if not label:
        return count_label
    return f"{label} ({count_label})"


def _normalize_headers(raw_headers) -> dict[str, str]:
    if not raw_headers:
        return {}
    if isinstance(raw_headers, Mapping):
        return {str(key): str(value) for key, value in raw_headers.items()}
    if isinstance(raw_headers, str):
        try:
            parsed = json.loads(raw_headers)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return {str(key): str(value) for key, value in parsed.items()}
        return {}
    return {}


_SUPPORTED_DRIVE_DOMAINS = DRIVE_SHARE_DOMAINS

_DRIVE_PROVIDER_LABELS = {
    "alipan.com": "阿里",
    "aliyundrive.com": "阿里",
    "mypikpak.com": "PikPak",
    "xunlei.com": "迅雷",
    "123pan.com": "123云盘",
    "123pan.cn": "123云盘",
    "123684.com": "123云盘",
    "123865.com": "123云盘",
    "123912.com": "123云盘",
    "123592.com": "123云盘",
    "quark.cn": "夸克",
    "139.com": "移动云盘",
    "uc.cn": "UC",
    "115.com": "115",
    "115cdn.com": "115",
    "anxia.com": "115",
    "189.cn": "天翼",
    "baidu.com": "百度",
    "guangyapan.com": "光鸭",
}


def _looks_like_drive_share_link(value: str) -> bool:
    return looks_like_drive_share_link(value)


def _detect_drive_provider_label(value: str) -> str:
    candidate = value.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return ""
    hostname = (urlparse(candidate).hostname or "").lower()
    for domain, label in _DRIVE_PROVIDER_LABELS.items():
        if hostname == domain or hostname.endswith(f".{domain}"):
            return label
    return ""


def _infer_external_subtitle_format(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".srt"):
        return "application/x-subrip"
    if path.endswith(".ass"):
        return "text/x-ass"
    if path.endswith(".ssa"):
        return "text/x-ssa"
    if path.endswith(".vtt"):
        return "text/vtt"
    return ""


def _subtitle_suffix_for_format(format_name: str) -> str:
    normalized = str(format_name or "").strip().lower()
    if normalized == "application/x-subrip":
        return ".srt"
    if normalized == "text/x-ass":
        return ".ass"
    if normalized == "text/x-ssa":
        return ".ssa"
    if normalized == "text/vtt":
        return ".vtt"
    return ""


def _map_spider_playback_qualities(
    payload: object,
    selected_url: str,
) -> tuple[list[VideoQualityOption], str]:
    if not isinstance(payload, list):
        return [], ""
    qualities: list[VideoQualityOption] = []
    selected_quality_id = ""
    for raw_quality in payload:
        if not isinstance(raw_quality, Mapping):
            continue
        quality_id = str(raw_quality.get("id") or "").strip()
        label = str(raw_quality.get("label") or "").strip()
        quality_url = str(raw_quality.get("url") or "").strip()
        if not quality_id or not label or not _looks_like_media_url(quality_url):
            continue
        qualities.append(VideoQualityOption(id=quality_id, label=label, url=quality_url))
        if not selected_quality_id and quality_url == selected_url:
            selected_quality_id = quality_id
    if not qualities:
        return [], ""
    return qualities, selected_quality_id or qualities[0].id


def _map_playback_detail_actions(payload: object) -> list[PlaybackDetailAction]:
    if not isinstance(payload, list):
        return []
    actions: list[PlaybackDetailAction] = []
    for raw_action in payload:
        if not isinstance(raw_action, Mapping):
            continue
        action_id = str(raw_action.get("id") or "").strip()
        label = str(raw_action.get("label") or "").strip()
        if not action_id or not label:
            continue
        action = PlaybackDetailAction(
            id=action_id,
            label=label,
            active=bool(raw_action.get("active")),
            enabled=bool(raw_action.get("enabled", True)),
            visible=bool(raw_action.get("visible", True)),
            tooltip=str(raw_action.get("tooltip") or "").strip(),
        )
        if action.visible:
            actions.append(action)
    return actions


def _map_playback_detail_fields(payload: object) -> list[PlaybackDetailField]:
    if not isinstance(payload, list):
        return []
    fields: list[PlaybackDetailField] = []
    for raw_field in payload:
        if not isinstance(raw_field, Mapping):
            continue
        label = str(raw_field.get("label") or "").strip()
        value_parts = _map_playback_detail_field_value_parts(raw_field.get("value"))
        if not label or not value_parts:
            continue
        fields.append(PlaybackDetailField(label=label, value_parts=value_parts))
    return fields


def _map_playback_detail_field_action(payload: object) -> PlaybackDetailFieldAction | None:
    if not isinstance(payload, Mapping):
        return None
    action_type = str(payload.get("type") or "").strip()
    value = str(payload.get("value") or "").strip()
    if action_type not in {"category", "detail", "search", "link"} or not value:
        return None
    return PlaybackDetailFieldAction(type=action_type, value=value)


def _map_playback_detail_field_value_parts(payload: object) -> list[PlaybackDetailValuePart]:
    if isinstance(payload, list):
        parts: list[PlaybackDetailValuePart] = []
        for raw_item in payload:
            if isinstance(raw_item, Mapping):
                label = str(raw_item.get("label") or "").strip()
                if not label:
                    continue
                parts.append(
                    PlaybackDetailValuePart(
                        label=label,
                        action=_map_playback_detail_field_action(raw_item.get("action")),
                    )
                )
                continue
            label = str(raw_item or "").strip()
            if label:
                parts.append(PlaybackDetailValuePart(label=label))
        return parts
    label = str(payload or "").strip()
    return [PlaybackDetailValuePart(label=label)] if label else []


def _merge_playback_detail_actions(
    collection_actions: list[PlaybackDetailAction],
    item_actions: list[PlaybackDetailAction],
) -> list[PlaybackDetailAction]:
    merged: list[PlaybackDetailAction] = list(collection_actions)
    seen_ids = {action.id for action in merged}
    for action in item_actions:
        if action.id in seen_ids:
            merged = [action if existing.id == action.id else existing for existing in merged]
            continue
        merged.append(action)
        seen_ids.add(action.id)
    return merged


def _move_spider_subtitle_to_cache(source_path: Path) -> Path:
    cache_dir = app_cache_dir() / "subtitles"
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        if source_path.resolve().is_relative_to(cache_dir.resolve()):
            return source_path
    except OSError:
        return source_path
    target_path = cache_dir / source_path.name
    if target_path.exists():
        stem = source_path.stem
        suffix = source_path.suffix
        index = 1
        while True:
            candidate = cache_dir / f"{stem}-{index}{suffix}"
            if not candidate.exists():
                target_path = candidate
                break
            index += 1
    return Path(shutil.move(str(source_path), str(target_path)))


def _write_inline_spider_subtitle_to_cache(format_name: str, text: str) -> Path:
    cache_dir = app_cache_dir() / "subtitles"
    cache_dir.mkdir(parents=True, exist_ok=True)
    suffix = _subtitle_suffix_for_format(format_name)
    digest = hashlib.sha256(f"{format_name}\n{text}".encode("utf-8")).hexdigest()
    target_path = cache_dir / f"inline_{digest}{suffix}"
    if not target_path.exists():
        target_path.write_text(text, encoding="utf-8")
    return target_path


def _write_inline_spider_karaoke_to_cache(text: str) -> Path:
    cache_dir = app_cache_dir() / "subtitles"
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    target_path = cache_dir / f"karaoke_{digest}.ass"
    if not target_path.exists():
        target_path.write_text(text, encoding="utf-8")
    return target_path


def _format_drive_route_label(route: str, provider: str) -> str:
    normalized_route = route.strip()
    if not provider or provider in normalized_route:
        return normalized_route
    return f"{normalized_route}({provider})"


def _split_grouped_route_label(route_label: str) -> tuple[str, str]:
    normalized = route_label.strip()
    if not normalized:
        return "", ""
    if any(char.isspace() for char in normalized):
        return normalized, normalized
    match = _GROUPED_ROUTE_RE.match(normalized)
    if match is None:
        return normalized, normalized
    group_label = match.group("group").strip()
    if not group_label:
        return normalized, normalized
    return group_label, normalized


def coerce_spider_plugin_action(payload: object) -> SpiderPluginAction | None:
    if not isinstance(payload, dict):
        return None
    action_id = str(payload.get("id") or "").strip()
    label = str(payload.get("label") or "").strip()
    if not action_id or not label:
        return None
    return SpiderPluginAction(
        id=action_id,
        label=label,
        enabled=bool(payload.get("enabled", True)),
        visible=bool(payload.get("visible", True)),
        tooltip=str(payload.get("tooltip") or "").strip(),
    )


class SpiderPluginController:
    def __init__(
        self,
        spider,
        plugin_name: str,
        search_enabled: bool,
        category_overrides_json: str = "",
        drive_detail_loader: Callable[[str], dict] | None = None,
        offline_download_detail_loader: Callable[[str], dict] | None = None,
        playback_history_loader: Callable[[str], object | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
        playback_parser_service=None,
        yt_dlp_service=None,
        preferred_parse_key_loader: Callable[[], str] | None = None,
        danmaku_service=None,
        danmaku_preference_store=None,
        base_url_loader: Callable[[], str] | None = None,
        metadata_hydrator_factory: Callable[..., object] | None = None,
        metadata_scrape_service_factory: Callable[..., object] | None = None,
        episode_title_enhancer_factory: Callable[..., object] | None = None,
        plugin_log_writer: Callable[[str], None] | None = None,
        spider_initializer: Callable[[], None] | None = None,
    ) -> None:
        self.uses_result_length_for_pagination = False
        self.uses_page_count_for_pagination = True
        self._spider = spider
        self._plugin_name = plugin_name
        self.supports_search = bool(search_enabled and callable(getattr(self._spider, "searchContent", None)))
        self._spider_initializer = spider_initializer
        self._spider_initialized = spider_initializer is None
        self._spider_init_lock = threading.Lock()
        self._drive_detail_loader = drive_detail_loader
        self._offline_download_detail_loader = offline_download_detail_loader
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver
        self._category_overrides_json = str(category_overrides_json or "")
        self._category_overrides = parse_category_overrides_json(self._category_overrides_json)
        self._playback_parser_service = playback_parser_service
        self._yt_dlp_service = yt_dlp_service
        self._preferred_parse_key_loader = preferred_parse_key_loader
        self._base_url_loader = base_url_loader
        self._metadata_hydrator_factory = metadata_hydrator_factory
        self._metadata_scrape_service_factory = metadata_scrape_service_factory
        self._episode_title_enhancer_factory = episode_title_enhancer_factory
        self._plugin_log_writer = plugin_log_writer
        self._danmaku_service = danmaku_service
        self._danmaku_preference_store = danmaku_preference_store
        self._danmaku_enabled = (
            bool(getattr(self._spider, "danmaku", lambda: False)()) if self._spider_initialized else False
        )
        self._danmaku_content_supported = (
            self._probe_danmaku_content_support() if self._spider_initialized else False
        )
        self._danmaku_lock = threading.Lock()
        self._pending_danmaku_item_ids: set[int] = set()
        self._warming_danmaku_source_cache_keys: set[str] = set()
        self._inflight_danmaku_source_searches: dict[str, _InflightDanmakuSourceSearch] = {}
        self._danmaku_prefetch_generation = 0
        self._danmaku_log_handler: Callable[[str], None] | None = None
        self._home_loaded = False
        self._raw_home_categories: list[SpiderPluginRawCategory] = []
        self._has_home_recommendations = False
        self._home_items: list[VodItem] = []
        self._items_by_vod_id: dict[str, VodItem] = {}
        self._search_supports_category = self._detect_search_supports_category()

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

    def _ensure_spider_initialized(self) -> None:
        if self._spider_initialized:
            return
        with self._spider_init_lock:
            if self._spider_initialized:
                return
            if self._spider_initializer is not None:
                self._spider_initializer()
            self._danmaku_enabled = bool(getattr(self._spider, "danmaku", lambda: False)())
            self._danmaku_content_supported = self._probe_danmaku_content_support()
            self._spider_initialized = True

    def _probe_danmaku_content_support(self) -> bool:
        probe = getattr(self._spider, "supports_danmaku_content", None)
        if callable(probe):
            try:
                return bool(probe())
            except Exception:
                logger.exception(
                    "Spider plugin danmakuContent support probe failed plugin=%s",
                    self._plugin_name,
                )
                return False
        return callable(getattr(self._spider, "danmakuContent", None))

    def _direct_danmaku_available(self, item: PlayItem) -> bool:
        if item.danmaku_source is not None:
            return True
        return self._danmaku_content_supported and bool(str(item.vod_id or "").strip())

    def _is_short_drama_item(self, item: PlayItem) -> bool:
        if _is_short_drama_item(item):
            return True
        plugin_name = str(self._plugin_name or "").strip()
        return "短剧" in plugin_name and any(marker in plugin_name for marker in ("目录", "合集", "更新"))

    def _log_spider_method_call(self, method_name: str, **params: object) -> None:
        message = _format_spider_method_call_message(self._plugin_name, method_name, **params)
        logger.info(message, extra={"log_category": "plugin", "log_source": "app"})
        if self._plugin_log_writer is None:
            return
        try:
            self._plugin_log_writer(message)
        except Exception:
            logger.exception("Spider plugin log sink failed plugin=%s method=%s", self._plugin_name, method_name)

    def _map_items(self, payload: dict) -> list[VodItem]:
        return [_map_item(item) for item in payload.get("list", [])]

    def _category_name_for_category_id(self, category_id: str) -> str:
        normalized_category_id = str(category_id or "").strip()
        if not normalized_category_id or normalized_category_id == "home":
            return ""
        for category in apply_category_overrides(self._raw_home_categories, self._category_overrides):
            if str(category.type_id or "").strip() == normalized_category_id:
                return str(category.type_name or "").strip()
        return ""

    def _annotate_items_with_category(self, items: list[VodItem], *, category_id: str = "") -> list[VodItem]:
        fallback_category_name = self._category_name_for_category_id(category_id)
        annotated: list[VodItem] = []
        for item in items:
            if fallback_category_name and not str(item.category_name or "").strip():
                item.category_name = fallback_category_name
            annotated.append(item)
            if item.vod_id:
                self._items_by_vod_id[str(item.vod_id)] = replace(item)
        return annotated

    def _apply_cached_item_metadata(self, detail: VodItem, source_vod_id: str) -> VodItem:
        fallback = self._items_by_vod_id.get(str(source_vod_id or "").strip())
        if fallback is None:
            fallback = self._items_by_vod_id.get(str(detail.vod_id or "").strip())
        if fallback is None:
            return detail
        if not str(detail.type_name or "").strip():
            detail.type_name = str(fallback.type_name or "").strip()
        if not str(detail.category_name or "").strip():
            detail.category_name = str(fallback.category_name or "").strip()
        return detail

    def _detect_search_supports_category(self) -> bool:
        if not self.supports_search:
            return False
        try:
            params = inspect.signature(self._spider.searchContent).parameters
        except (AttributeError, TypeError, ValueError) as e:
            logger.debug("Spider plugin search signature introspection failed plugin=%s error=%s", self._plugin_name, e)
            return True
        return "category" in params

    def _ensure_home_loaded(self) -> None:
        self._ensure_spider_initialized()
        if self._home_loaded:
            return
        try:
            payload = self._spider.homeContent(True) or {}
        except Exception as exc:
            logger.exception("Spider plugin home load failed plugin=%s", self._plugin_name)
            raise ApiError(str(exc)) from exc
        raw_filters = payload.get("filters") or {}
        self._raw_home_categories = [
            SpiderPluginRawCategory(
                type_id=_coerce_category_id(item.get("type_id")),
                type_name=str(item.get("type_name") or ""),
                filters=_map_category_filters(raw_filters.get(_coerce_category_id(item.get("type_id")))),
            )
            for item in payload.get("class", [])
        ]
        items = self._map_items(payload)
        if items:
            items = self._annotate_items_with_category(items)
        self._has_home_recommendations = bool(items)
        self._home_items = items
        self._home_loaded = True

    def _normalize_spider_subtitle_url(self, value: object) -> str:
        raw_value = str(value or "")
        if not raw_value.strip():
            return ""
        format_name, separator, subtitle_text = raw_value.partition("\n")
        if separator:
            format_name = format_name.strip()
            suffix = _subtitle_suffix_for_format(format_name)
            if suffix and subtitle_text.strip():
                return str(_write_inline_spider_subtitle_to_cache(format_name, subtitle_text))
        raw = raw_value.strip()
        if raw.startswith(("http://", "https://")):
            return raw
        local_path = Path(raw)
        if local_path.is_absolute() and local_path.exists():
            return str(_move_spider_subtitle_to_cache(local_path))
        if not raw.startswith("/"):
            return ""
        base_url = "" if self._base_url_loader is None else str(self._base_url_loader() or "").strip()
        if not base_url:
            return ""
        return urljoin(f"{base_url.rstrip('/')}/", raw.lstrip("/"))

    def _map_spider_external_subtitles(self, payload: object) -> list[ExternalSubtitleOption]:
        url = self._normalize_spider_subtitle_url(payload)
        if not url:
            return []
        return [
            ExternalSubtitleOption(
                name="外挂字幕 [插件]",
                lang="",
                url=url,
                format=_infer_external_subtitle_format(url),
                source="spider",
            )
        ]

    def _map_spider_karaoke_subtitle(self, payload: object) -> list[ExternalSubtitleOption]:
        if not isinstance(payload, Mapping):
            return []
        format_name = str(payload.get("format") or "").strip()
        text = str(payload.get("text") or "")
        translation = str(payload.get("translation") or "")
        if not format_name or not text.strip():
            return []
        document = parse_raw_karaoke(format_name, text, translation=translation)
        if not document.lines:
            return []
        subtitle_path = _write_inline_spider_karaoke_to_cache(render_karaoke_ass(document))
        return [
            ExternalSubtitleOption(
                name="逐字歌词 [插件]",
                lang="",
                url=str(subtitle_path),
                format="text/x-ass",
                source="spider",
            )
        ]

    def load_raw_categories(self) -> list[DoubanCategory]:
        self._ensure_home_loaded()
        return [
            DoubanCategory(type_id=item.type_id, type_name=item.type_name, filters=list(item.filters))
            for item in self._raw_home_categories
        ]

    def manager_actions(self) -> list[SpiderPluginAction]:
        get_actions = getattr(self._spider, "getManagerActions", None)
        if not callable(get_actions):
            return []
        self._ensure_spider_initialized()
        actions: list[SpiderPluginAction] = []
        for payload in get_actions() or []:
            action = coerce_spider_plugin_action(payload)
            if action is None:
                if self._plugin_log_writer is not None:
                    self._plugin_log_writer(f"插件动作声明无效: {payload!r}")
                continue
            if action.visible:
                actions.append(action)
        return actions

    def load_categories(self) -> list[DoubanCategory]:
        self._ensure_home_loaded()
        categories = apply_category_overrides(self._raw_home_categories, self._category_overrides)
        if self._has_home_recommendations:
            categories = [DoubanCategory(type_id="home", type_name="推荐"), *categories]
        return categories

    def load_items(
        self,
        category_id: str,
        page: int,
        filters: dict[str, str] | None = None,
    ) -> tuple[list[VodItem], int]:
        self._ensure_home_loaded()
        if category_id == "home":
            return list(self._home_items), 1
        return self._load_category_items(category_id, page, filters=filters)

    def load_folder_items(self, vod_id: str, page: int = 1) -> tuple[list[VodItem], int]:
        self._ensure_home_loaded()
        return self._load_category_items(str(vod_id or ""), page, filters={})

    def _load_category_items(
        self,
        category_id: str,
        page: int,
        filters: dict[str, str] | None = None,
    ) -> tuple[list[VodItem], int]:
        try:
            self._log_spider_method_call(
                "categoryContent",
                tid=category_id,
                pg=page,
                filter=False,
                extend=dict(filters or {}),
            )
            payload = self._spider.categoryContent(category_id, page, False, dict(filters or {})) or {}
        except Exception as exc:
            logger.exception(
                "Spider plugin category load failed plugin=%s category_id=%s page=%s",
                self._plugin_name,
                category_id,
                page,
            )
            raise ApiError(str(exc)) from exc
        items = self._annotate_items_with_category(self._map_items(payload), category_id=category_id)
        total = _map_spider_page_count(payload, items)
        self._log_spider_method_call(
            "categoryContent",
            items=len(items),
            pagecount=total,
        )
        return items, total

    def search_items(
        self,
        keyword: str,
        page: int,
        category_id: str = "",
    ) -> tuple[list[VodItem], int]:
        self._ensure_spider_initialized()
        if not self.supports_search:
            raise ApiError("当前插件不支持搜索")
        category = "" if category_id == "home" else str(category_id or "")
        try:
            if self._search_supports_category:
                self._log_spider_method_call(
                    "searchContent",
                    key=keyword,
                    quick=False,
                    pg=page,
                    category=category,
                )
                payload = self._spider.searchContent(keyword, False, page, category)
            else:
                self._log_spider_method_call(
                    "searchContent",
                    key=keyword,
                    quick=False,
                    pg=page,
                )
                payload = self._spider.searchContent(keyword, False, page)
            payload = payload or {}
        except Exception as exc:
            logger.exception(
                "Spider plugin search failed plugin=%s keyword=%s page=%s category_id=%s",
                self._plugin_name,
                keyword,
                page,
                category,
            )
            raise ApiError(str(exc)) from exc
        items = self._annotate_items_with_category(self._map_items(payload), category_id=category_id)
        total = _map_spider_page_count(payload, items)
        return items, total

    def _route_name(self, routes: list[str], group_index: int) -> str:
        route = routes[group_index] if group_index < len(routes) else ""
        route = route.strip()
        return route or f"线路 {group_index + 1}"

    def _build_playlist(self, detail: VodItem) -> list[list[PlayItem]]:
        routes = [item.strip() for item in (detail.vod_play_from or "").split("$$$")]
        groups = (detail.vod_play_url or "").split("$$$")
        playlists: list[list[PlayItem]] = []
        for group_index, group in enumerate(groups):
            route = self._route_name(routes, group_index)
            route_label = route
            playlist: list[PlayItem] = []
            for raw_chunk in group.split("#"):
                chunk = raw_chunk.strip()
                if not chunk:
                    continue
                title, separator, value = chunk.partition("$")
                if not separator:
                    title = chunk
                    value = chunk
                clean_value = value.strip()
                is_drive_link = _looks_like_drive_share_link(clean_value)
                is_media_url = _looks_like_media_url(clean_value) and not is_drive_link
                if is_drive_link:
                    provider = _detect_drive_provider_label(clean_value)
                    if provider:
                        route_label = _format_drive_route_label(route, provider)
                playlist.append(
                    PlayItem(
                        title=title.strip() or clean_value or f"选集 {len(playlist) + 1}",
                        url=clean_value if is_media_url else "",
                        media_title=detail.vod_name,
                        path=detail.vod_id if is_drive_link else "",
                        vod_id="" if is_media_url else clean_value,
                        index=len(playlist),
                        play_source=route_label,
                        type_name=detail.type_name,
                        category_name=detail.category_name,
                    )
                )
            if playlist:
                playlists.append(seed_original_titles(_mark_short_bare_numeric_playlist(playlist)))
        return playlists

    def _build_grouped_play_item(self, detail: VodItem, raw_media: Mapping[object, object]) -> PlayItem | None:
        display_name = str(raw_media.get("name") or "").strip()
        raw_url = coerce_media_url(raw_media.get("url"))
        if not raw_url:
            return None
        title = display_name or raw_url
        is_drive_link = _looks_like_drive_share_link(raw_url)
        is_media_url = _looks_like_media_url(raw_url) and not is_drive_link
        return PlayItem(
            title=title,
            url=raw_url if is_media_url else "",
            original_title=title,
            media_title=detail.vod_name,
            path=detail.vod_id if is_drive_link else "",
            vod_id="" if is_media_url else raw_url,
            index=0,
            play_source=title,
            type_name=detail.type_name,
            category_name=detail.category_name,
        )

    def _build_grouped_sources_from_payload(
        self,
        detail: VodItem,
        payload: object,
    ) -> tuple[list[PlaybackSourceGroup], list[list[PlayItem]]]:
        if not isinstance(payload, list):
            return [], []
        source_groups: list[PlaybackSourceGroup] = []
        playlists: list[list[PlayItem]] = []
        for raw_group in payload:
            if not isinstance(raw_group, Mapping):
                continue
            group_name = str(raw_group.get("name") or "").strip()
            raw_media_list = raw_group.get("media")
            if not group_name or not isinstance(raw_media_list, list):
                continue
            sources: list[PlaybackSource] = []
            for raw_media in raw_media_list:
                if not isinstance(raw_media, Mapping):
                    continue
                item = self._build_grouped_play_item(detail, raw_media)
                if item is None:
                    continue
                playlist = seed_original_titles([item])
                playlists.append(playlist)
                sources.append(PlaybackSource(label=item.title, playlist=playlist))
            if sources:
                source_groups.append(PlaybackSourceGroup(label=group_name, sources=sources))
        return source_groups, playlists

    def _build_source_groups_from_playlists(self, playlists: list[list[PlayItem]]) -> list[PlaybackSourceGroup]:
        source_groups: list[PlaybackSourceGroup] = []
        group_index_by_label: dict[str, int] = {}
        for playlist_index, playlist in enumerate(playlists):
            route_label = self._route_name([playlist[0].play_source if playlist else ""], 0)
            group_label, source_label = _split_grouped_route_label(route_label)
            if group_label not in group_index_by_label:
                group_index_by_label[group_label] = len(source_groups)
                source_groups.append(PlaybackSourceGroup(label=group_label, sources=[]))
            source_groups[group_index_by_label[group_label]].sources.append(
                PlaybackSource(label=source_label, playlist=playlist)
            )
        return source_groups

    def _resolve_drive_grouped(
        self,
        session: PlayerSession,
        item: PlayItem,
        url: str,
    ) -> PlaybackLoadResult | None:
        # New per-directory resolve for spider-plugin drive items. Returns a grouped,
        # lazily-loadable result; returns None to fall back to the legacy flattened loader.
        # resolve_drive/list_drive_files are derived from the bound drive_detail_loader
        # (its __self__ is the ApiClient) to avoid threading new deps through the plugin chain.
        client = getattr(self._drive_detail_loader, "__self__", None)
        resolver = getattr(client, "resolve_drive", None) if client is not None else None
        if resolver is None:
            return None
        files_loader = getattr(client, "list_drive_files", None)
        media_title = item.media_title or item.title or url
        try:
            result = resolver(url, media_title) or {}
        except Exception as exc:
            logger.warning(
                "Spider plugin drive resolve failed plugin=%s source=%s",
                self._plugin_name,
                url,
                exc_info=exc,
            )
            return None
        resource_id = str(result.get("resourceId") or "")
        detail = VodItem(vod_id=url, vod_name=str(result.get("vodName") or media_title))
        source_groups, playlists = build_drive_grouped_sources(
            detail,
            resource_id,
            result.get("directories") or [],
            result.get("files") or [],
            files_loader,
        )
        if not source_groups or not any(playlists):
            return None
        start_index = self._resolve_replacement_start_index(item.path or url, playlists[0])
        logger.info(
            "Spider plugin resolved drive (grouped) plugin=%s source=%s groups=%s",
            self._plugin_name,
            url,
            len(source_groups),
        )
        return PlaybackLoadResult(
            replacement_playlist=playlists[0],
            replacement_start_index=start_index,
            source_groups=source_groups,
            playlists=playlists,
            drive_resource_id=resource_id,
            drive_files_loader=files_loader,
        )

    def _build_drive_replacement_playlist(
        self,
        detail: VodItem,
        play_source: str,
        media_title: str = "",
        category_name: str = "",
    ) -> list[PlayItem]:
        resolved_media_title = media_title.strip() or detail.vod_name
        resolved_category_name = detail.category_name or category_name
        if detail.items:
            return seed_original_titles(_mark_short_bare_numeric_playlist([
                PlayItem(
                    title=item.title,
                    url=item.url,
                    media_title=resolved_media_title,
                    path=item.path,
                    index=index,
                    size=item.size,
                    vod_id=item.vod_id,
                    headers=dict(item.headers),
                    play_source=play_source,
                    type_name=detail.type_name or item.type_name,
                    category_name=resolved_category_name or item.category_name,
                    metadata_provider_url=extract_official_link_url(detail.detail_fields),
                )
                for index, item in enumerate(detail.items)
                if item.url
            ]))
        playlist = build_detail_playlist(detail)
        return seed_original_titles(_mark_short_bare_numeric_playlist([
            PlayItem(
                title=item.title,
                url=item.url,
                media_title=resolved_media_title,
                path=item.path,
                index=index,
                size=item.size,
                vod_id=item.vod_id,
                headers=dict(item.headers),
                play_source=play_source,
                type_name=detail.type_name or item.type_name,
                category_name=resolved_category_name or item.category_name,
                metadata_provider_url=extract_official_link_url(detail.detail_fields),
            )
            for index, item in enumerate(playlist)
            if item.url and not _looks_like_drive_share_link(item.url)
        ]))

    def _resolve_folder_like_detail(self, item: PlayItem) -> VodItem | None:
        if not item.vod_id or self._drive_detail_loader is None:
            return None
        try:
            payload = self._drive_detail_loader(item.vod_id)
            return _map_vod_item(payload["list"][0])
        except (KeyError, IndexError):
            return None

    def _build_offline_download_replacement_playlist(
        self,
        detail: VodItem,
        play_source: str,
        media_title: str = "",
        source_item: PlayItem | None = None,
    ) -> list[PlayItem]:
        playlist = build_detail_playlist(detail)
        resolved_media_title = media_title.strip() or detail.vod_name
        source_episode = ""
        if source_item is not None:
            source_episode = source_item.danmaku_search_episode.strip() or _extract_episode_label(source_item).strip()
        replacement_playlist = seed_original_titles(_mark_short_bare_numeric_playlist([
            PlayItem(
                title=item.title,
                url=item.url,
                media_title=resolved_media_title,
                path=item.path,
                index=index,
                size=item.size,
                vod_id=item.vod_id,
                headers=dict(item.headers),
                play_source=play_source,
                type_name=detail.type_name or item.type_name,
                category_name=detail.category_name or item.category_name,
            )
            for index, item in enumerate(playlist)
            if item.url or item.vod_id
        ]))
        for item in replacement_playlist:
            item.danmaku_search_title = _normalize_default_danmaku_search_title(item.media_title) or (
                _normalize_default_danmaku_search_title(item.title)
            )
            item.danmaku_search_episode = source_episode or _extract_episode_label(item, replacement_playlist)
            item.danmaku_search_query = _compose_danmaku_search_query(
                item.danmaku_search_title,
                item.danmaku_search_episode,
            )
        return replacement_playlist

    def _apply_single_offline_download_item(self, current_item: PlayItem, replacement: PlayItem) -> None:
        current_item.vod_id = replacement.vod_id
        current_item.path = replacement.path
        current_item.size = replacement.size
        current_item.headers = dict(replacement.headers)
        current_item.external_subtitles = list(replacement.external_subtitles)
        current_item.playback_qualities = list(replacement.playback_qualities)
        current_item.selected_playback_quality_id = replacement.selected_playback_quality_id
        if replacement.url:
            current_item.url = replacement.url

    def _replace_current_playlist_item(
        self,
        playlist: list[PlayItem],
        current_item: PlayItem,
        replacements: list[PlayItem],
    ) -> tuple[list[PlayItem], int]:
        if not playlist:
            updated = list(replacements)
            for index, item in enumerate(updated):
                item.index = index
            return updated, 0
        try:
            current_index = playlist.index(current_item)
        except ValueError:
            current_index = max(0, min(int(current_item.index or 0), len(playlist) - 1))
        updated = list(playlist[:current_index]) + list(replacements) + list(playlist[current_index + 1 :])
        for index, item in enumerate(updated):
            item.index = index
        return updated, current_index

    def _resolve_danmaku_sync(
        self,
        item: PlayItem,
        url: str,
        playlist: list[PlayItem] | None = None,
        *,
        is_prefetch: bool = False,
        prefetch_still_valid: Callable[[], bool] | None = None,
    ) -> None:
        if self._direct_danmaku_available(item) and self._resolve_direct_danmaku(
            item,
            playlist,
            is_prefetch=is_prefetch,
            prefetch_still_valid=prefetch_still_valid,
        ):
            return
        if not self._danmaku_enabled or self._danmaku_service is None or self._is_short_drama_item(item):
            return
        is_prefetch_valid = prefetch_still_valid or (lambda: True)
        lookup = self._prepare_danmaku_lookup(item, url, playlist)
        if lookup is None:
            return
        preference, search_name, reg_src = lookup
        if not is_prefetch_valid():
            return
        if self._apply_cached_danmaku(item, search_name, reg_src, preference, playlist=playlist):
            if not is_prefetch_valid():
                return
            logger.info(
                "Spider plugin loaded cached danmaku plugin=%s source=%s",
                self._plugin_name,
                item.vod_id,
            )
            return
        if not is_prefetch:
            self._log_danmaku_event("弹幕搜索中", detail=search_name)
        try:
            default_url = self._populate_danmaku_candidates(item, search_name, reg_src, playlist=playlist)
        except Exception as exc:
            logger.warning(
                "Spider plugin danmaku search failed plugin=%s source=%s error=%s",
                self._plugin_name,
                item.vod_id,
                exc,
            )
            return
        if not is_prefetch:
            candidate_count = sum(len(group.options) for group in item.danmaku_candidates)
            self._log_danmaku_event("弹幕搜索成功", detail=f"找到 {candidate_count} 个候选")
        candidates = self._iter_danmaku_candidate_options(item.danmaku_candidates, default_url)
        if not candidates:
            return
        explicit_episode_requested, requested_episode = _query_requests_explicit_episode(search_name)
        provider_label_by_key = {group.provider: group.provider_label for group in item.danmaku_candidates}
        last_error = ""
        for candidate in candidates:
            if explicit_episode_requested and requested_episode is not None:
                candidate_episode = extract_episode_number(candidate.name)
                if (
                    candidate_episode is not None
                    and candidate_episode != requested_episode
                    and episode_title_matches(search_name, candidate.name)
                ):
                    continue
            try:
                if not is_prefetch_valid():
                    return
                item.selected_danmaku_provider = candidate.provider
                item.selected_danmaku_url = candidate.url
                item.selected_danmaku_title = candidate.name
                platform_label = provider_label_by_key.get(candidate.provider) or candidate.provider
                candidate_label = (candidate.name or candidate.url or "").strip()
                download_detail = f"{platform_label} - {candidate_label}" if platform_label else candidate_label
                if not is_prefetch:
                    self._log_danmaku_event("弹幕下载中", detail=download_detail)
                cached_candidate_xml = load_cached_danmaku_xml(search_name, candidate.url)
                resolved_xml = cached_candidate_xml or self._resolve_danmaku_xml(
                    candidate.url, candidate
                )
                if not is_prefetch_valid():
                    return
                item.danmaku_xml = resolved_xml
                self._save_danmaku_xml_cache(
                    item,
                    search_name,
                    reg_src,
                    item.danmaku_xml,
                    playlist,
                    page_url=candidate.url,
                )
                self._save_danmaku_source_preference(item)
                self._schedule_series_level_danmaku_source_cache_warm(item, reg_src, playlist)
                if not is_prefetch and item.danmaku_xml:
                    danmaku_count = _count_danmaku_entries(item.danmaku_xml)
                    self._log_danmaku_event(
                        "弹幕下载成功",
                        detail=_build_danmaku_success_detail(item, danmaku_count, playlist),
                    )
                logger.info(
                    "Spider plugin resolved danmaku plugin=%s source=%s candidate=%s",
                    self._plugin_name,
                    item.vod_id,
                    candidate.url,
                )
                return
            except Exception as exc:
                logger.warning(
                    "Spider plugin danmaku candidate failed plugin=%s source=%s candidate=%s error=%s",
                    self._plugin_name,
                    item.vod_id,
                    candidate.url,
                    exc,
                )
                last_error = str(exc)
                item.danmaku_error = last_error
        if not is_prefetch and last_error:
            self._log_danmaku_event("弹幕下载失败", detail=last_error)

    def _lookup_selected_danmaku_title(self, groups: list[DanmakuSourceGroup], page_url: str) -> str:
        for group in groups:
            for option in group.options:
                if option.url == page_url:
                    return option.name
        return ""

    def _resolve_danmaku_search_title(
        self,
        item: PlayItem,
        preference: DanmakuSeriesPreference | None = None,
    ) -> str:
        if item.danmaku_search_title.strip():
            return item.danmaku_search_title.strip()
        default_title = (
            _normalize_default_danmaku_search_title(item.media_title)
            or _normalize_default_danmaku_search_title(item.title)
        )
        if preference is not None and preference.search_title.strip():
            preferred_title = preference.search_title.strip()
            if not _should_prefer_default_danmaku_search_title(default_title, preferred_title):
                return preferred_title
        return default_title

    def _resolve_danmaku_search_episode(
        self,
        item: PlayItem,
        playlist: list[PlayItem] | None = None,
    ) -> str:
        return item.danmaku_search_episode.strip() or _extract_episode_label(item, playlist).strip()

    def resolve_default_danmaku_search_episode(
        self,
        item: PlayItem,
        playlist: list[PlayItem] | None = None,
    ) -> str:
        """Dialog-facing recompute of the default episode label.

        前置流程（网盘替换播放列表、自动搜索等）可能把集数留空；弹幕源对话框打开
        时用它兜底重算，保证集数框有值、搜索带集数锚点。
        """
        return _extract_episode_label(item, playlist).strip()

    def _save_danmaku_search_title_preference(self, item: PlayItem) -> None:
        if self._danmaku_preference_store is None or not item.danmaku_series_key:
            return
        existing = self._danmaku_preference_store.load(item.danmaku_series_key)
        self._danmaku_preference_store.save(
            DanmakuSeriesPreference(
                series_key=item.danmaku_series_key,
                provider=existing.provider if existing is not None else "",
                page_url=existing.page_url if existing is not None else "",
                title=existing.title if existing is not None else "",
                search_title=item.danmaku_search_title,
                updated_at=int(time.time()),
                episode_source_offsets=(
                    existing.episode_source_offsets if existing is not None else {}
                ),
            )
        )

    def _metadata_danmaku_preference(
        self,
        item: PlayItem,
        series_key: str,
    ) -> DanmakuSeriesPreference | None:
        """Synthesize a danmaku preference from metadata's official-link URL.

        When the user has no saved source choice for this series, metadata
        hydration may still tell us the official platform (e.g. a ``v.qq.com``
        cover URL). Pin that provider so the search hits it directly instead of
        blind keyword search across all providers. Returns None if metadata
        offers no recognizable platform URL.
        """
        metadata_url = str(item.metadata_provider_url or "").strip()
        provider_key = match_provider(metadata_url) if metadata_url else None
        if provider_key is None:
            return None
        return DanmakuSeriesPreference(
            series_key=series_key,
            provider=provider_key,
            page_url=metadata_url,
            title="",
        )

    def _prepare_danmaku_lookup(
        self,
        item: PlayItem,
        url: str,
        playlist: list[PlayItem] | None = None,
    ) -> tuple[DanmakuSeriesPreference | None, str, str] | None:
        series_key = build_danmaku_series_key(item.media_title or item.title)
        preference = self._danmaku_preference_store.load(series_key) if self._danmaku_preference_store is not None else None
        if preference is None:
            preference = self._metadata_danmaku_preference(item, series_key)
        search_title = self._resolve_danmaku_search_title(item, preference)
        search_episode = self._resolve_danmaku_search_episode(item, playlist)
        search_name = _compose_danmaku_search_query(search_title, search_episode)
        if not search_name:
            return None
        item.danmaku_series_key = series_key
        item.danmaku_search_title = search_title
        item.danmaku_search_episode = search_episode
        if not item.danmaku_search_query_overridden or not item.danmaku_search_query:
            item.danmaku_search_query = search_name
        reg_src = str(item.vod_id or url or "").strip()
        return preference, search_name, reg_src

    def _apply_cached_danmaku(
        self,
        item: PlayItem,
        query_name: str,
        reg_src: str,
        preference: DanmakuSeriesPreference | None,
        playlist: list[PlayItem] | None = None,
    ) -> bool:
        cached_xml = load_cached_danmaku_xml(query_name, reg_src)
        if cached_xml:
            logger.info(
                "Spider plugin loaded cached danmaku xml directly plugin=%s query=%s reg_src=%s title=%s",
                self._plugin_name,
                query_name,
                reg_src,
                item.title,
            )
            item.danmaku_xml = cached_xml
            return True
        if self._apply_cached_danmaku_preference_xml(item, query_name, preference):
            logger.info(
                "Spider plugin loaded cached danmaku via preference plugin=%s source=%s page_url=%s",
                self._plugin_name,
                item.vod_id,
                item.selected_danmaku_url,
            )
            return True
        if self._apply_cached_danmaku_candidate_xml(item, query_name, playlist):
            logger.info(
                "Spider plugin loaded cached danmaku via cached candidate plugin=%s source=%s page_url=%s",
                self._plugin_name,
                item.vod_id,
                item.selected_danmaku_url,
            )
            return True
        return False

    def _apply_cached_danmaku_candidate_xml(
        self,
        item: PlayItem,
        query_name: str,
        playlist: list[PlayItem] | None = None,
    ) -> bool:
        if not self.load_cached_danmaku_sources(item, playlist=playlist):
            return False
        explicit_episode_requested, requested_episode = _query_requests_explicit_episode(query_name)
        logger.info(
            (
                "Spider plugin inspect cached danmaku candidates plugin=%s query=%s title=%s "
                "explicit_episode=%s requested_episode=%s selected_url=%s candidate_groups=%s"
            ),
            self._plugin_name,
            query_name,
            item.title,
            explicit_episode_requested,
            requested_episode,
            item.selected_danmaku_url,
            len(item.danmaku_candidates),
        )
        for candidate in self._iter_danmaku_candidate_options(item.danmaku_candidates, item.selected_danmaku_url):
            candidate_episode = extract_episode_number(candidate.name)
            episode_match = (
                requested_episode is not None
                and candidate_episode == requested_episode
                and episode_title_matches(query_name, candidate.name)
            )
            cached_xml = load_cached_danmaku_xml(query_name, candidate.url)
            logger.info(
                (
                    "Spider plugin inspect cached danmaku candidate plugin=%s query=%s candidate=%s "
                    "candidate_episode=%s episode_match=%s cached=%s"
                ),
                self._plugin_name,
                query_name,
                candidate.url,
                candidate_episode,
                episode_match,
                bool(cached_xml),
            )
            if not cached_xml:
                continue
            if explicit_episode_requested and not episode_match:
                logger.info(
                    (
                        "Spider plugin ignore cached danmaku candidate xml plugin=%s query=%s candidate=%s "
                        "candidate_title=%s reason=no_matching_episode"
                    ),
                    self._plugin_name,
                    query_name,
                    candidate.url,
                    candidate.name,
                )
                continue
            item.danmaku_xml = cached_xml
            item.selected_danmaku_provider = candidate.provider
            item.selected_danmaku_url = candidate.url
            item.selected_danmaku_title = candidate.name
            item.danmaku_error = ""
            logger.info(
                "Spider plugin loaded cached danmaku via candidate plugin=%s query=%s candidate=%s candidate_title=%s",
                self._plugin_name,
                query_name,
                candidate.url,
                candidate.name,
            )
            return True
        return False

    def _save_danmaku_source_preference(self, item: PlayItem) -> None:
        if self._danmaku_preference_store is None or not item.danmaku_series_key:
            return
        existing = self._danmaku_preference_store.load(item.danmaku_series_key)
        self._danmaku_preference_store.save(
            DanmakuSeriesPreference(
                series_key=item.danmaku_series_key,
                provider=item.selected_danmaku_provider or (existing.provider if existing is not None else ""),
                page_url=item.selected_danmaku_url or (existing.page_url if existing is not None else ""),
                title=item.selected_danmaku_title or (existing.title if existing is not None else ""),
                search_title=item.danmaku_search_title or (existing.search_title if existing is not None else ""),
                updated_at=int(time.time()),
                episode_source_offsets=(
                    existing.episode_source_offsets if existing is not None else {}
                ),
            )
        )

    def _schedule_series_level_danmaku_source_cache_warm(
        self,
        item: PlayItem,
        reg_src: str,
        playlist: list[PlayItem] | None = None,
    ) -> None:
        if self._danmaku_service is None or not hasattr(self._danmaku_service, "search_danmu_sources"):
            return
        search_title = str(item.danmaku_search_title or "").strip()
        provider_filter = str(item.selected_danmaku_provider or "").strip()
        if not search_title or not provider_filter:
            return
        query_name = str(item.danmaku_search_query or "").strip()
        query_requests_episode, _ = _query_requests_explicit_episode(query_name)
        if not query_requests_episode:
            return
        title_key = _compact_danmaku_title(search_title)
        for group in item.danmaku_candidates:
            if group.provider != provider_filter:
                continue
            related_options = [
                option
                for option in group.options
                if extract_episode_number(option.name) is not None
                and (
                    not title_key
                    or title_key in _compact_danmaku_title(strip_episode_suffix(option.name))
                )
            ]
            if len(related_options) > 1:
                return
        cache_query_names = _danmaku_cache_query_names(item, search_title, playlist)
        preferred_page_url = str(item.selected_danmaku_url or "").strip()
        target_duration = int(item.duration_seconds or 0)
        cache_warm_key = f"{search_title}\n{provider_filter}"
        with self._danmaku_lock:
            if cache_warm_key in self._warming_danmaku_source_cache_keys:
                return
            self._warming_danmaku_source_cache_keys.add(cache_warm_key)

        def run() -> None:
            try:
                search_method = self._danmaku_service.search_danmu_sources
                search_kwargs = {
                    "preferred_provider": provider_filter,
                    "preferred_page_url": preferred_page_url,
                    "media_duration_seconds": target_duration,
                }
                if "provider_filter" in inspect.signature(search_method).parameters:
                    search_kwargs["provider_filter"] = provider_filter
                result = search_method(search_title, reg_src, **search_kwargs)
                if not getattr(result, "groups", None):
                    return
                for cache_query_name in cache_query_names:
                    _save_cached_danmaku_source_search_result_variants(cache_query_name, reg_src, result)
                logger.info(
                    (
                        "Spider plugin warmed series-level danmaku source cache plugin=%s title=%s "
                        "provider=%s groups=%s"
                    ),
                    self._plugin_name,
                    search_title,
                    provider_filter,
                    len(result.groups),
                )
            except Exception as exc:
                logger.warning(
                    (
                        "Spider plugin warm danmaku source cache failed plugin=%s title=%s "
                        "provider=%s error=%s"
                    ),
                    self._plugin_name,
                    search_title,
                    provider_filter,
                    exc,
                )
            finally:
                with self._danmaku_lock:
                    self._warming_danmaku_source_cache_keys.discard(cache_warm_key)

        threading.Thread(target=run, daemon=True).start()

    def _apply_cached_danmaku_preference_xml(
        self,
        item: PlayItem,
        query_name: str,
        preference: DanmakuSeriesPreference | None,
    ) -> bool:
        if preference is None or not preference.page_url:
            return False
        if not _cached_danmaku_title_matches_query(query_name, preference.title):
            logger.info(
                (
                    "Spider plugin ignore cached danmaku preference xml plugin=%s query=%s "
                    "page_url=%s title=%s reason=no_matching_episode"
                ),
                self._plugin_name,
                query_name,
                preference.page_url,
                preference.title,
            )
            return False
        cached_xml = load_cached_danmaku_xml(query_name, preference.page_url)
        if not cached_xml:
            return False
        item.danmaku_xml = cached_xml
        item.selected_danmaku_provider = preference.provider
        item.selected_danmaku_url = preference.page_url
        item.selected_danmaku_title = preference.title
        return True

    def _iter_danmaku_candidate_options(
        self,
        groups: list[DanmakuSourceGroup],
        default_url: str,
    ) -> list[DanmakuSourceOption]:
        ordered: list[DanmakuSourceOption] = []
        fallback: list[DanmakuSourceOption] = []
        for group in groups:
            for option in group.options:
                if option.url == default_url and default_url:
                    ordered.append(option)
                else:
                    fallback.append(option)
        ordered.extend(fallback)
        return ordered

    def _populate_danmaku_candidates(
        self,
        item: PlayItem,
        query_name: str,
        reg_src: str,
        force_refresh: bool = False,
        media_duration_seconds: int = 0,
        playlist: list[PlayItem] | None = None,
        provider_filter: str = "",
    ) -> str:
        def execute_source_search(search_name: str):
            if hasattr(self._danmaku_service, "search_danmu_sources"):
                search_method = self._danmaku_service.search_danmu_sources
                search_kwargs = {
                    "preferred_provider": preference.provider if preference is not None else "",
                    "preferred_page_url": preference.page_url if preference is not None else "",
                    "media_duration_seconds": target_duration,
                }
                if "provider_filter" in inspect.signature(search_method).parameters:
                    search_kwargs["provider_filter"] = provider_filter
                return search_method(search_name, reg_src, **search_kwargs)
            candidates = self._danmaku_service.search_danmu(search_name, reg_src, provider_filter=provider_filter)
            return self._legacy_source_search_result(candidates)

        series_key = build_danmaku_series_key(item.media_title or query_name)
        target_duration = media_duration_seconds if media_duration_seconds > 0 else int(item.duration_seconds or 0)
        item.danmaku_series_key = series_key
        item.danmaku_search_query = query_name
        item.danmaku_search_provider = provider_filter
        preference = self._danmaku_preference_store.load(series_key) if self._danmaku_preference_store is not None else None
        item.danmaku_search_title = self._resolve_danmaku_search_title(item, preference)
        item.danmaku_search_episode = self._resolve_danmaku_search_episode(item, playlist)
        logger.info(
            (
                "Spider plugin populate danmaku candidates plugin=%s title=%s query=%s search_title=%s "
                "search_episode=%s reg_src=%s provider_filter=%s duration=%s"
            ),
            self._plugin_name,
            item.title,
            query_name,
            item.danmaku_search_title,
            item.danmaku_search_episode,
            reg_src,
            provider_filter,
            target_duration,
        )
        if not item.danmaku_search_query_overridden or not item.danmaku_search_query:
            item.danmaku_search_query = _compose_danmaku_search_query(item.danmaku_search_title, item.danmaku_search_episode)
        else:
            item.danmaku_search_query = query_name
        query_name = item.danmaku_search_query
        if (
            not provider_filter
            and not force_refresh
            and self.load_cached_danmaku_sources(item, media_duration_seconds=target_duration, playlist=playlist)
        ):
            return item.selected_danmaku_url
        requested_query_name = query_name
        explicit_episode_requested, _ = _query_requests_explicit_episode(requested_query_name)
        inflight_search_key = ""
        inflight_search: _InflightDanmakuSourceSearch | None = None
        owns_inflight_search = False
        if (
            not force_refresh
            and item.danmaku_search_title
            and explicit_episode_requested
        ):
            inflight_search_key = f"{item.danmaku_search_title.strip()}\n{provider_filter.strip()}"
            inflight_search, owns_inflight_search = self._acquire_inflight_danmaku_source_search(inflight_search_key)
            if inflight_search is not None and not owns_inflight_search:
                inflight_search.event.wait()
                joined_result = inflight_search.result
                if joined_result is not None and _cached_danmaku_source_result_matches_query(requested_query_name, joined_result):
                    result = self._rerank_danmaku_source_search_result_for_item(
                        joined_result,
                        item=item,
                        query_name=requested_query_name,
                        reg_src=reg_src,
                        preferred_provider=preference.provider if preference is not None else "",
                        preferred_page_url=preference.page_url if preference is not None else "",
                        media_duration_seconds=target_duration,
                    )
                    self._apply_danmaku_source_search_result(item, result)
                    if result.groups and item.danmaku_search_title:
                        self._save_danmaku_search_title_preference(item)
                    return result.default_option_url
        try:
            result = execute_source_search(requested_query_name)
            fallback_query_name = item.danmaku_search_title.strip()
            if (
                hasattr(self._danmaku_service, "search_danmu_sources")
                and not result.groups
                and fallback_query_name
                and fallback_query_name != requested_query_name
                and _should_retry_danmaku_search_title_only(item)
            ):
                result = execute_source_search(fallback_query_name)
                result = self._rerank_danmaku_source_search_result_for_item(
                    result,
                    item=item,
                    query_name=requested_query_name,
                    reg_src=reg_src,
                    preferred_provider=preference.provider if preference is not None else "",
                    preferred_page_url=preference.page_url if preference is not None else "",
                    media_duration_seconds=target_duration,
                )
                result = _prioritize_requested_episode_in_source_search_result(requested_query_name, result)
            if inflight_search is not None and owns_inflight_search:
                inflight_search.result = result
        except Exception as exc:
            if inflight_search is not None and owns_inflight_search:
                inflight_search.error = exc
            raise
        finally:
            if inflight_search is not None and owns_inflight_search:
                inflight_search.event.set()
                self._release_inflight_danmaku_source_search(inflight_search_key, inflight_search)
        if not provider_filter and not is_other_fallback_only_result(result):
            for cache_query_name in _danmaku_cache_query_names(item, requested_query_name, playlist):
                _save_cached_danmaku_source_search_result_variants(cache_query_name, reg_src, result)
        self._apply_danmaku_source_search_result(item, result)
        if result.groups and item.danmaku_search_title:
            self._save_danmaku_search_title_preference(item)
        return result.default_option_url

    def _save_danmaku_xml_cache(
        self,
        item: PlayItem,
        query_name: str,
        reg_src: str,
        xml_text: str,
        playlist: list[PlayItem] | None = None,
        page_url: str = "",
    ) -> None:
        for cache_query_name in _danmaku_cache_query_names(item, query_name, playlist):
            save_cached_danmaku_xml(cache_query_name, reg_src, xml_text)
            if page_url:
                save_cached_danmaku_xml(cache_query_name, page_url, xml_text)

    def _resolve_danmaku_xml(
        self,
        page_url: str,
        option: DanmakuSourceOption | None = None,
    ) -> str:
        resolve_method = self._danmaku_service.resolve_danmu
        kwargs = {}
        if option is not None and "option" in inspect.signature(resolve_method).parameters:
            kwargs["option"] = option
        return resolve_method(page_url, **kwargs)

    def _direct_danmaku_cache_keys(self, item: PlayItem) -> tuple[str, str]:
        return (
            f"plugin:{self._plugin_name}",
            f"danmaku-direct:{str(item.vod_id or '').strip()}",
        )

    def _direct_danmaku_headers(self, item: PlayItem, source: SpiderDanmakuDirectSource) -> dict[str, str]:
        headers = {
            key: value
            for key, value in (item.headers or {}).items()
            if str(key).lower() in _SPIDER_DANMAKU_HEADER_WHITELIST and value
        }
        for key, value in (source.headers or {}).items():
            if value:
                headers[str(key)] = str(value)
        return headers

    def _fetch_direct_danmaku_xml(
        self,
        item: PlayItem,
        source: SpiderDanmakuDirectSource,
        *,
        is_prefetch: bool,
    ) -> str:
        if source.xml:
            return source.xml
        if source.url:
            if not is_prefetch:
                self._log_danmaku_event("弹幕下载中", item, detail=source.url)
            response = httpx.get(
                source.url,
                headers=self._direct_danmaku_headers(item, source),
                timeout=_SPIDER_DANMAKU_FETCH_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
            response.raise_for_status()
            return str(response.text or "").strip()
        if not is_prefetch:
            self._log_danmaku_event("弹幕下载中", item, detail="插件直供")
        self._log_spider_method_call("danmakuContent", flag=item.play_source, id=item.vod_id)
        result = self._spider.danmakuContent(item.play_source, item.vod_id)
        self._log_spider_method_call("danmakuContent", response=result)
        return str(result or "").strip()

    def _resolve_direct_danmaku(
        self,
        item: PlayItem,
        playlist: list[PlayItem] | None = None,
        *,
        is_prefetch: bool = False,
        prefetch_still_valid: Callable[[], bool] | None = None,
    ) -> bool:
        """直供弹幕(playerContent danmaku 字段或 danmakuContent 方法),成功返回 True。"""
        source = item.danmaku_source or SpiderDanmakuDirectSource()
        is_prefetch_valid = prefetch_still_valid or (lambda: True)
        cache_name, cache_reg = self._direct_danmaku_cache_keys(item)
        cached_xml = load_cached_danmaku_xml(cache_name, cache_reg)
        if _spider_danmaku_xml_valid(cached_xml):
            self._apply_direct_danmaku(item, cached_xml, source, playlist=playlist, is_prefetch=is_prefetch)
            logger.info(
                "Spider plugin loaded cached direct danmaku plugin=%s source=%s",
                self._plugin_name,
                item.vod_id,
            )
            return True
        if not is_prefetch_valid():
            return False
        error = ""
        try:
            xml_text = self._fetch_direct_danmaku_xml(item, source, is_prefetch=is_prefetch)
        except Exception as exc:
            error = str(exc)
            logger.warning(
                "Spider plugin direct danmaku failed plugin=%s source=%s error=%s",
                self._plugin_name,
                item.vod_id,
                exc,
            )
            xml_text = ""
        if not is_prefetch_valid():
            return False
        if not _spider_danmaku_xml_valid(xml_text):
            if error:
                item.danmaku_error = error
                if not is_prefetch:
                    self._log_danmaku_event("弹幕下载失败", item, detail=error)
            else:
                logger.info(
                    "Spider plugin direct danmaku empty plugin=%s source=%s",
                    self._plugin_name,
                    item.vod_id,
                )
            return False
        save_cached_danmaku_xml(cache_name, cache_reg, xml_text)
        self._apply_direct_danmaku(item, xml_text, source, playlist=playlist, is_prefetch=is_prefetch)
        logger.info(
            "Spider plugin resolved direct danmaku plugin=%s source=%s url=%s",
            self._plugin_name,
            item.vod_id,
            source.url,
        )
        return True

    def _apply_direct_danmaku(
        self,
        item: PlayItem,
        xml_text: str,
        source: SpiderDanmakuDirectSource,
        *,
        playlist: list[PlayItem] | None = None,
        is_prefetch: bool = False,
    ) -> None:
        item.danmaku_xml = xml_text
        item.selected_danmaku_provider = f"plugin:{self._plugin_name}"
        item.selected_danmaku_url = source.url
        item.selected_danmaku_title = (item.media_title or item.title).strip()
        item.danmaku_error = ""
        if not is_prefetch:
            danmaku_count = _count_danmaku_entries(xml_text)
            self._log_danmaku_event(
                "弹幕下载成功",
                item,
                detail=_build_danmaku_success_detail(item, danmaku_count, playlist),
            )

    def _apply_danmaku_source_search_result(self, item: PlayItem, result) -> None:
        item.danmaku_candidates = result.groups
        item.selected_danmaku_provider = result.default_provider
        item.selected_danmaku_url = result.default_option_url
        item.selected_danmaku_title = self._lookup_selected_danmaku_title(result.groups, result.default_option_url)
        item.danmaku_error = ""

    def _acquire_inflight_danmaku_source_search(
        self,
        key: str,
    ) -> tuple[_InflightDanmakuSourceSearch | None, bool]:
        normalized_key = str(key or "").strip()
        if not normalized_key:
            return None, False
        with self._danmaku_lock:
            existing = self._inflight_danmaku_source_searches.get(normalized_key)
            if existing is not None:
                return existing, False
            created = _InflightDanmakuSourceSearch(event=threading.Event())
            self._inflight_danmaku_source_searches[normalized_key] = created
            return created, True

    def _release_inflight_danmaku_source_search(
        self,
        key: str,
        search: _InflightDanmakuSourceSearch | None,
    ) -> None:
        normalized_key = str(key or "").strip()
        if not normalized_key or search is None:
            return
        with self._danmaku_lock:
            existing = self._inflight_danmaku_source_searches.get(normalized_key)
            if existing is search:
                self._inflight_danmaku_source_searches.pop(normalized_key, None)

    def _rerank_danmaku_source_search_result_for_item(
        self,
        result,
        *,
        item: PlayItem,
        query_name: str,
        reg_src: str,
        preferred_provider: str = "",
        preferred_page_url: str = "",
        media_duration_seconds: int = 0,
    ):
        if hasattr(self._danmaku_service, "rerank_danmaku_source_search_result"):
            return self._danmaku_service.rerank_danmaku_source_search_result(
                result,
                query_name=query_name,
                reg_src=reg_src,
                preferred_provider=preferred_provider,
                preferred_page_url=preferred_page_url,
                media_duration_seconds=media_duration_seconds,
            )
        return result

    def load_cached_danmaku_sources(
        self,
        item: PlayItem,
        playlist: list[PlayItem] | None = None,
        media_duration_seconds: int = 0,
    ) -> bool:
        series_key = build_danmaku_series_key(item.media_title or item.title or item.danmaku_search_query)
        preference = self._danmaku_preference_store.load(series_key) if self._danmaku_preference_store is not None else None
        item.danmaku_search_title = self._resolve_danmaku_search_title(item, preference)
        item.danmaku_search_episode = self._resolve_danmaku_search_episode(item, playlist)
        query_name = (item.danmaku_search_query or _compose_danmaku_search_query(item.danmaku_search_title, item.danmaku_search_episode)).strip()
        if not query_name:
            return False
        item.danmaku_series_key = series_key
        item.danmaku_search_query = query_name
        reg_src = str(item.vod_id or item.url or "").strip()
        cached_result = _load_cached_danmaku_source_search_result_variants(query_name, reg_src)
        if cached_result is None:
            cached_result = _load_series_level_cached_danmaku_source_result(item, reg_src)
        if cached_result is not None and is_stale_other_fallback_result(cached_result, reg_src):
            # 历史污染条目：other 兜底候选指向别的 reg_src，直接弃用，走新搜索。
            cached_result = None
        logger.info(
            "Spider plugin load cached danmaku sources plugin=%s title=%s query=%s reg_src=%s cache_hit=%s",
            self._plugin_name,
            item.title,
            query_name,
            reg_src,
            cached_result is not None,
        )
        if cached_result is None:
            return False
        if not _cached_danmaku_source_result_matches_query(query_name, cached_result):
            logger.info(
                (
                    "Spider plugin ignore stale cached danmaku sources plugin=%s title=%s query=%s reg_src=%s "
                    "reason=no_matching_episode"
                ),
                self._plugin_name,
                item.title,
                query_name,
                reg_src,
            )
            return False
        target_duration = media_duration_seconds if media_duration_seconds > 0 else int(item.duration_seconds or 0)
        if hasattr(self._danmaku_service, "rerank_danmaku_source_search_result"):
            cached_result = self._danmaku_service.rerank_danmaku_source_search_result(
                cached_result,
                query_name=query_name,
                reg_src=reg_src,
                preferred_provider=preference.provider if preference is not None else "",
                preferred_page_url=preference.page_url if preference is not None else "",
                media_duration_seconds=target_duration,
            )
        self._apply_danmaku_source_search_result(item, cached_result)
        return True

    def _legacy_source_search_result(self, candidates: list) -> object:
        groups: dict[str, list[DanmakuSourceOption]] = {}
        for item in candidates:
            groups.setdefault(item.provider, []).append(
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
        source_groups = [
            DanmakuSourceGroup(provider=provider, provider_label=provider, options=options)
            for provider, options in groups.items()
        ]
        default_option = source_groups[0].options[0] if source_groups and source_groups[0].options else None
        from atv_player.danmaku.models import DanmakuSourceSearchResult

        return DanmakuSourceSearchResult(
            groups=source_groups,
            default_option_url=default_option.url if default_option is not None else "",
            default_provider=default_option.provider if default_option is not None else "",
        )

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
        if search_title_override is None and search_episode_override is None and query_override is not None:
            query_name = query_override.strip()
            if not query_name:
                return
            item.danmaku_search_query = query_name
            item.danmaku_search_query_overridden = True
        else:
            series_key = build_danmaku_series_key(item.media_title or item.title)
            preference = self._danmaku_preference_store.load(series_key) if self._danmaku_preference_store is not None else None
            item.danmaku_search_title = (
                search_title_override.strip()
                if search_title_override is not None
                else self._resolve_danmaku_search_title(item, preference)
            )
            item.danmaku_search_episode = (
                search_episode_override.strip()
                if search_episode_override is not None
                else self._resolve_danmaku_search_episode(item, playlist)
            )
            query_name = _compose_danmaku_search_query(item.danmaku_search_title, item.danmaku_search_episode)
            item.danmaku_search_query = query_name
            item.danmaku_search_query_overridden = (
                search_title_override is not None or search_episode_override is not None or query_override is not None
            )
        if not query_name:
            return
        reg_src = str(item.vod_id or item.url or "").strip()
        self._log_danmaku_event("弹幕搜索中", detail=query_name)
        self._populate_danmaku_candidates(
            item,
            query_name,
            reg_src,
            force_refresh=force_refresh,
            media_duration_seconds=media_duration_seconds,
            playlist=playlist,
            provider_filter=provider_filter,
        )
        candidate_count = sum(len(group.options) for group in item.danmaku_candidates)
        self._log_danmaku_event("弹幕搜索成功", detail=f"找到 {candidate_count} 个候选")

    def auto_resolve_danmaku(
        self,
        item: PlayItem,
        playlist: list[PlayItem] | None = None,
        *,
        media_duration_seconds: int = 0,
    ) -> bool:
        """Find and download the default danmaku source for one playlist item.

        Mirrors ``GenericDanmakuController.auto_resolve_danmaku`` so the player
        window's generic auto-load fallback (``_maybe_restore_cached_danmaku_for_current_item``)
        can search and download danmaku for spider-plugin sources, not just restore
        from a cached source. Without this, opening a plugin item from playback
        history — where the loader-based ``_maybe_resolve_danmaku`` does not always
        run for the resumed episode — left danmaku unloaded until a manual search.
        """
        if item.danmaku_xml:
            return True
        query_override = item.danmaku_search_query if item.danmaku_search_query_overridden else None
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

    def switch_danmaku_source(self, item: PlayItem, page_url: str) -> str:
        selected_option = None
        selected_provider_label = ""
        for group in item.danmaku_candidates:
            for option in group.options:
                if option.url == page_url:
                    selected_option = option
                    selected_provider_label = group.provider_label
                    break
            if selected_option is not None:
                break
        option_label = (selected_option.name if selected_option is not None else "").strip()
        download_detail = option_label
        if selected_provider_label and option_label:
            download_detail = f"{selected_provider_label} - {option_label}"
        elif selected_provider_label:
            download_detail = selected_provider_label
        self._log_danmaku_event("弹幕下载中", detail=download_detail)
        query_name = (item.danmaku_search_query or _build_danmaku_search_name(item)).strip()
        cached_xml = load_cached_danmaku_xml(query_name, page_url)
        xml_text = cached_xml or self._resolve_danmaku_xml(page_url, selected_option)
        item.danmaku_xml = xml_text
        item.selected_danmaku_url = page_url
        item.selected_danmaku_title = self._lookup_selected_danmaku_title(item.danmaku_candidates, page_url)
        for group in item.danmaku_candidates:
            for option in group.options:
                if option.url == page_url:
                    item.selected_danmaku_provider = option.provider
                    break
        query_name = (item.danmaku_search_query or _build_danmaku_search_name(item)).strip()
        reg_src = str(item.vod_id or item.url or "").strip()
        self._save_danmaku_xml_cache(item, query_name, reg_src, xml_text, page_url=page_url)
        self._save_danmaku_source_preference(item)
        self._schedule_series_level_danmaku_source_cache_warm(item, reg_src)
        danmaku_count = _count_danmaku_entries(xml_text)
        self._log_danmaku_event("弹幕下载成功", detail=_build_danmaku_success_detail(item, danmaku_count))
        return xml_text

    def download_danmaku_from_url(self, item: PlayItem, page_url: str) -> str:
        normalized_url = normalize_danmaku_episode_url(page_url)
        danmaku_service = self._danmaku_service
        if danmaku_service is None:
            raise RuntimeError("弹幕服务不可用")
        provider_key = danmaku_service.provider_key_for_url(normalized_url)
        source_title = (item.title or item.media_title or "单集弹幕").strip()
        self._log_danmaku_event("弹幕下载中", detail=f"{provider_key} - {source_title}")
        query_name = (item.danmaku_search_query or _build_danmaku_search_name(item)).strip()
        reg_src = str(item.vod_id or item.url or "").strip()
        xml_text = load_cached_danmaku_xml(query_name, normalized_url)
        if not xml_text:
            xml_text = self._resolve_danmaku_xml(normalized_url)
        self._save_danmaku_xml_cache(
            item,
            query_name,
            reg_src,
            xml_text,
            page_url=normalized_url,
        )
        item.danmaku_xml = xml_text
        item.selected_danmaku_provider = provider_key
        item.selected_danmaku_url = normalized_url
        item.selected_danmaku_title = source_title
        item.danmaku_error = ""
        self._save_danmaku_source_preference(item)
        danmaku_count = _count_danmaku_entries(xml_text)
        self._log_danmaku_event(
            "弹幕下载成功",
            detail=_build_danmaku_success_detail(item, danmaku_count),
        )
        return xml_text

    def prefetch_next_episode_danmaku(
        self,
        item: PlayItem,
        playlist: list[PlayItem],
    ) -> None:
        self._ensure_spider_initialized()
        direct_available = self._direct_danmaku_available(item)
        if not direct_available:
            if self._is_short_drama_item(item):
                return
            if not _should_prefetch_danmaku(item, playlist):
                return
        url = (item.url or item.vod_id or "").strip()
        if not url:
            return
        if item.danmaku_xml or item.danmaku_pending:
            return
        if not direct_available and self._danmaku_enabled and self._danmaku_service is not None:
            lookup = self._prepare_danmaku_lookup(item, url, playlist)
            if lookup is not None:
                preference, search_name, reg_src = lookup
                if self._apply_cached_danmaku(item, search_name, reg_src, preference, playlist=playlist):
                    return
        prefetch_label = _build_danmaku_search_name(item, playlist) or (item.media_title or item.title)
        self._log_danmaku_event("弹幕预下载中", detail=prefetch_label)
        self._maybe_resolve_danmaku(item, url, playlist, is_prefetch=True)

    def invalidate_running_danmaku_prefetches(self) -> None:
        with self._danmaku_lock:
            self._danmaku_prefetch_generation += 1

    def set_danmaku_log_handler(self, handler: Callable[[str], None] | None) -> None:
        self._danmaku_log_handler = handler

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

    def _maybe_resolve_danmaku(self, item: PlayItem, url: str, playlist: list[PlayItem] | None = None, *, is_prefetch: bool = False) -> None:
        # 直供(payload danmaku 字段/danmakuContent 方法)不受聚合开关、弹幕服务与短剧排除约束
        if not self._direct_danmaku_available(item) and (
            not self._danmaku_enabled or self._danmaku_service is None or self._is_short_drama_item(item)
        ):
            return
        if item.danmaku_xml or item.danmaku_pending:
            return
        item_id = id(item)
        with self._danmaku_lock:
            if item_id in self._pending_danmaku_item_ids:
                return
            self._pending_danmaku_item_ids.add(item_id)
            prefetch_generation = self._danmaku_prefetch_generation
        item.danmaku_pending = True

        def prefetch_still_valid() -> bool:
            if not is_prefetch:
                return True
            with self._danmaku_lock:
                return prefetch_generation == self._danmaku_prefetch_generation

        def run() -> None:
            try:
                self._resolve_danmaku_sync(
                    item,
                    url,
                    playlist,
                    is_prefetch=is_prefetch,
                    prefetch_still_valid=prefetch_still_valid,
                )
                if is_prefetch and item.danmaku_xml and prefetch_still_valid():
                    danmaku_count = _count_danmaku_entries(item.danmaku_xml)
                    self._log_danmaku_event(
                        "弹幕预下载成功",
                        detail=_build_danmaku_success_detail(item, danmaku_count, playlist),
                    )
            finally:
                item.danmaku_pending = False
                with self._danmaku_lock:
                    self._pending_danmaku_item_ids.discard(item_id)

        threading.Thread(target=run, daemon=True).start()

    def _resolve_replacement_start_index(self, vod_id: str, replacement: list[PlayItem]) -> int:
        if self._playback_history_loader is None or not replacement:
            return 0
        history = self._playback_history_loader(vod_id)
        return resolve_resume_index(history, replacement, 0)

    def _run_detail_action(
        self,
        vod: VodItem,
        playlists: list[list[PlayItem]],
        playlist_index: int,
        item: PlayItem,
        action_id: str,
    ) -> list[PlaybackDetailAction]:
        self._ensure_spider_initialized()
        runner = getattr(self._spider, "runPlayerAction", None)
        if not callable(runner):
            raise ValueError(f"详情动作未注册[{action_id}]")
        context = {
            "action_id": action_id,
            "vod": vod,
            "play_item": item,
            "playlist": playlists[playlist_index] if 0 <= playlist_index < len(playlists) else [],
            "playlist_index": playlist_index,
            "play_index": item.index,
            "log": lambda message: logger.info(
                "Spider detail action plugin=%s action=%s %s",
                self._plugin_name,
                action_id,
                message,
            ),
        }
        payload = runner(action_id, context)
        refreshed_actions: list[PlaybackDetailAction]
        if isinstance(payload, Mapping):
            refreshed_actions = _map_playback_detail_actions(payload.get("actions"))
        else:
            refreshed_actions = _map_playback_detail_actions(payload)
        return _merge_playback_detail_actions(item.detail_actions, refreshed_actions)

    def _resolve_play_item(self, session: PlayerSession, item: PlayItem) -> PlaybackLoadResult | None:
        self._ensure_spider_initialized()
        current_playlist = session.playlist if item in session.playlist else None
        if item.url:
            self._maybe_hydrate_ytdlp_item(item, item.url, vod=session.vod)
            session.video_cover_override = item.video_cover_override
            if not item.danmaku_xml:
                self._maybe_resolve_danmaku(item, item.url, current_playlist)
            return
        preserved_quality_id = item.selected_playback_quality_id if str(item.selected_playback_quality_id or "").startswith("ytdlp_") else ""
        item.external_subtitles = []
        item.playback_qualities = []
        item.selected_playback_quality_id = preserved_quality_id
        if not item.vod_id:
            return
        if _looks_like_offline_download_link(item.vod_id):
            if self._offline_download_detail_loader is None:
                raise ValueError("当前插件未配置磁力链接解析")
            try:
                payload = self._offline_download_detail_loader(item.vod_id)
                detail = _map_vod_item(payload["list"][0])
            except (KeyError, IndexError) as exc:
                logger.exception(
                    "Spider plugin offline download detail failed plugin=%s source=%s",
                    self._plugin_name,
                    item.vod_id,
                )
                raise ValueError(f"没有可播放的项目: {item.title or item.vod_id}") from exc
            replacement = self._build_offline_download_replacement_playlist(
                detail,
                item.play_source,
                media_title=item.media_title,
                source_item=item,
            )
            if not replacement:
                raise ValueError(f"没有可播放的项目: {detail.vod_name or item.title}")
            session.detail_resolver = self._resolve_folder_like_detail
            session.resolved_vod_by_id = {}
            merged_playlist, replacement_start_index = self._replace_current_playlist_item(
                session.playlist,
                item,
                replacement,
            )
            logger.info(
                "Spider plugin resolved offline download playlist plugin=%s source=%s items=%s",
                self._plugin_name,
                item.vod_id,
                len(replacement),
            )
            return PlaybackLoadResult(
                replacement_playlist=merged_playlist,
                replacement_start_index=replacement_start_index,
            )
        if _looks_like_drive_share_link(item.vod_id):
            grouped = self._resolve_drive_grouped(session, item, item.vod_id)
            if grouped is not None:
                return grouped
            if self._drive_detail_loader is None:
                raise ValueError("当前插件未配置网盘解析")
            try:
                payload = self._drive_detail_loader(item.vod_id)
                detail = _map_vod_item(payload["list"][0])
            except (KeyError, IndexError) as exc:
                logger.exception(
                    "Spider plugin drive detail failed plugin=%s source=%s",
                    self._plugin_name,
                    item.vod_id,
                )
                raise ValueError(f"没有可播放的项目: {item.title or item.vod_id}") from exc
            replacement = self._build_drive_replacement_playlist(
                detail,
                item.play_source,
                media_title=item.media_title,
                category_name=item.category_name,
            )
            if not replacement:
                raise ValueError(f"没有可播放的项目: {detail.vod_name or item.title}")
            replacement_start_index = self._resolve_replacement_start_index(item.path or detail.vod_id, replacement)
            replacement_item = replacement[replacement_start_index]
            if _should_auto_resolve_danmaku(replacement_item, replacement):
                self._maybe_resolve_danmaku(replacement_item, item.vod_id, replacement)
            logger.info(
                "Spider plugin resolved drive playlist plugin=%s source=%s items=%s",
                self._plugin_name,
                item.vod_id,
                len(replacement),
            )
            return PlaybackLoadResult(
                replacement_playlist=replacement,
                replacement_start_index=replacement_start_index,
            )
        try:
            self._log_spider_method_call(
                "playerContent",
                flag=item.play_source,
                id=item.vod_id,
                vipFlags=[],
            )
            payload = self._spider.playerContent(item.play_source, item.vod_id, []) or {}
            self._log_spider_method_call(
                "playerContent",
                response=payload,
            )
        except Exception as exc:
            logger.exception(
                "Spider plugin playback resolve failed plugin=%s source=%s",
                self._plugin_name,
                item.vod_id,
            )
            raise ValueError(str(exc)) from exc
        cover_source = str(payload.get("cover") or "").strip()
        item.danmaku_source = _map_spider_danmaku_source(payload.get("danmaku"))
        parse_required = int(payload.get("parse") or 0) == 1
        item.parse_required = parse_required
        raw_url = payload.get("url")
        url = coerce_media_url(raw_url)
        if not isinstance(raw_url, str) and url:
            logger.info(
                "playerContent returned array-format url, extracted media url plugin=%s source=%s",
                self._plugin_name,
                item.vod_id,
            )
        if _looks_like_offline_download_link(url):
            if self._offline_download_detail_loader is None:
                raise ValueError("当前插件未配置磁力链接解析")
            try:
                payload = self._offline_download_detail_loader(url)
                detail = _map_vod_item(payload["list"][0])
            except (KeyError, IndexError) as exc:
                logger.exception(
                    "Spider plugin offline download detail failed plugin=%s source=%s",
                    self._plugin_name,
                    url,
                )
                raise ValueError(f"没有可播放的项目: {item.title or item.vod_id}") from exc
            replacement = self._build_offline_download_replacement_playlist(
                detail,
                item.play_source,
                media_title=item.media_title,
                source_item=item,
            )
            if not replacement:
                raise ValueError(f"没有可播放的项目: {detail.vod_name or item.title}")
            session.detail_resolver = self._resolve_folder_like_detail
            session.resolved_vod_by_id = {}
            merged_playlist, replacement_start_index = self._replace_current_playlist_item(
                session.playlist,
                item,
                replacement,
            )
            logger.info(
                "Spider plugin resolved offline download playlist from playerContent plugin=%s source=%s items=%s",
                self._plugin_name,
                url,
                len(replacement),
            )
            if cover_source:
                replacement_item = merged_playlist[replacement_start_index]
                replacement_item.video_cover_override = cover_source
                session.video_cover_override = cover_source
            return PlaybackLoadResult(
                replacement_playlist=merged_playlist,
                replacement_start_index=replacement_start_index,
            )
        if _looks_like_drive_share_link(url):
            grouped = self._resolve_drive_grouped(session, item, url)
            if grouped is not None:
                return grouped
            if self._drive_detail_loader is None:
                raise ValueError("当前插件未配置网盘解析")
            try:
                payload = self._drive_detail_loader(url)
                detail = _map_vod_item(payload["list"][0])
            except (KeyError, IndexError) as exc:
                logger.exception(
                    "Spider plugin drive detail failed plugin=%s source=%s",
                    self._plugin_name,
                    item.vod_id,
                )
                raise ValueError(f"没有可播放的项目: {item.title or item.vod_id}") from exc
            replacement = self._build_drive_replacement_playlist(
                detail,
                item.play_source,
                media_title=item.media_title,
                category_name=item.category_name,
            )
            if not replacement:
                raise ValueError(f"没有可播放的项目: {detail.vod_name or item.title}")
            replacement_start_index = self._resolve_replacement_start_index(item.path or detail.vod_id, replacement)
            replacement_item = replacement[replacement_start_index]
            if _should_auto_resolve_danmaku(replacement_item, replacement):
                self._maybe_resolve_danmaku(replacement_item, url, replacement)
            logger.info(
                "Spider plugin resolved drive playlist plugin=%s source=%s items=%s",
                self._plugin_name,
                item.vod_id,
                len(replacement),
            )
            if cover_source:
                item.video_cover_override = cover_source
                replacement_item.video_cover_override = cover_source
            session.video_cover_override = replacement_item.video_cover_override or item.video_cover_override
            return PlaybackLoadResult(
                replacement_playlist=replacement,
                replacement_start_index=replacement_start_index,
            )
        if parse_required:
            if self._playback_parser_service is None:
                raise ValueError("当前插件未配置内置解析")
            result = self._playback_parser_service.resolve(
                item.play_source,
                url,
                preferred_key="" if self._preferred_parse_key_loader is None else self._preferred_parse_key_loader(),
            )
            item.url = result.url
            item.headers = dict(result.headers)
            if cover_source:
                item.video_cover_override = cover_source
            session.video_cover_override = item.video_cover_override
            self._maybe_resolve_danmaku(item, url, current_playlist)
            logger.info(
                "Spider plugin resolved parse playback plugin=%s source=%s parser=%s",
                self._plugin_name,
                item.vod_id,
                result.parser_key,
            )
            return None
        if not _looks_like_media_url(url):
            raise ValueError("插件未返回可播放地址")
        item.url = url
        item.original_url = item.original_url or url
        item.headers = _normalize_headers(payload.get("header"))
        item.detail_fields = _map_playback_detail_fields(payload.get("ext"))
        item.detail_actions = _merge_playback_detail_actions(
            item.detail_actions,
            _map_playback_detail_actions(payload.get("actions")),
        )
        item.external_subtitles = self._map_spider_karaoke_subtitle(payload.get("lyric"))
        if not item.external_subtitles:
            item.external_subtitles = self._map_spider_external_subtitles(payload.get("subt"))
        if not self._maybe_hydrate_ytdlp_item(item, url, vod=session.vod):
            item.playback_qualities, item.selected_playback_quality_id = _map_spider_playback_qualities(
                payload.get("qualities"),
                url,
            )
        if cover_source:
            item.video_cover_override = cover_source
        session.video_cover_override = item.video_cover_override
        self._maybe_resolve_danmaku(item, url, current_playlist)
        logger.info(
            "Spider plugin resolved playback url plugin=%s source=%s play_source=%s",
                self._plugin_name,
                item.vod_id,
            item.play_source,
        )
        return None

    def _maybe_hydrate_ytdlp_item(
        self,
        item: PlayItem,
        source_url: str,
        *,
        vod: VodItem | None = None,
    ) -> bool:
        yt_dlp = self._yt_dlp_service
        if yt_dlp is None or not yt_dlp.is_available():
            return False
        candidate = str(source_url or "").strip()
        if not candidate or not yt_dlp.can_resolve(candidate):
            return False
        selected_quality_id = item.selected_playback_quality_id or ""
        selected_audio_track_id = item.selected_audio_track_id or ""
        if selected_quality_id.startswith("ytdlp_"):
            result = yt_dlp.resolve_for_quality(
                candidate,
                selected_quality_id,
                audio_track_id=selected_audio_track_id,
            )
        else:
            result = yt_dlp.resolve(
                candidate,
                max_height=None,
                selected_audio_track_id=selected_audio_track_id,
            )
        yt_dlp.apply_result(result, vod=vod, item=item, source_url=candidate)
        return True

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        self._ensure_spider_initialized()
        try:
            self._log_spider_method_call(
                "detailContent",
                ids=[vod_id],
            )
            payload = self._spider.detailContent([vod_id]) or {}
        except Exception as exc:
            logger.exception("Spider plugin detail load failed plugin=%s vod_id=%s", self._plugin_name, vod_id)
            raise ValueError(str(exc)) from exc
        try:
            raw_detail = payload["list"][0]
            detail = _map_vod_item(raw_detail)
            detail = self._apply_cached_item_metadata(detail, vod_id)
            detail.detail_fields = _map_playback_detail_fields(raw_detail.get("ext") if isinstance(raw_detail, Mapping) else None)
        except (KeyError, IndexError) as exc:
            raise ValueError(f"没有可播放的项目: {vod_id}") from exc
        source_groups: list[PlaybackSourceGroup] = []
        playlists: list[list[PlayItem]] = []
        if isinstance(raw_detail, Mapping):
            source_groups, playlists = self._build_grouped_sources_from_payload(detail, raw_detail.get("group"))
        if not playlists:
            playlists = self._build_playlist(detail)
        collection_actions = _map_playback_detail_actions(raw_detail.get("actions") if isinstance(raw_detail, Mapping) else None)
        if collection_actions:
            for current_playlist in playlists:
                for item in current_playlist:
                    item.detail_actions = list(collection_actions)
        if not playlists:
            # 带上备注(如爬虫标注的"未开播")说明空列表原因, 只在确有备注时追加
            remarks = str(getattr(detail, "vod_remarks", "") or "").strip()
            raise ValueError(
                f"没有可播放的项目: {detail.vod_name}" + (f"（{remarks}）" if remarks else "")
            )
        logger.info(
            "Spider plugin build request plugin=%s vod_id=%s routes=%s",
            self._plugin_name,
            detail.vod_id,
            len(playlists),
        )
        playlist = playlists[0]
        source_vod_id = vod_id or detail.vod_id
        history_loader = None
        history_saver = None
        if self._playback_history_loader is not None:
            def history_loader(source_vod_id=source_vod_id, vod_name=str(detail.vod_name or "")):
                try:
                    return self._playback_history_loader(source_vod_id, vod_name)
                except TypeError:
                    # 兼容只收 vod_id 的旧注入方(测试桩等)
                    return self._playback_history_loader(source_vod_id)
        if self._playback_history_saver is not None:
            def history_saver(payload, source_vod_id=source_vod_id):
                return self._playback_history_saver(
                    source_vod_id,
                    payload,
                )

        def playback_loader(
            session_or_item: PlayerSession | PlayItem,
            item: PlayItem | None = None,
        ) -> PlaybackLoadResult | None:
            if item is None:
                session = PlayerSession(
                    vod=detail,
                    playlist=playlist,
                    start_index=0,
                    start_position_seconds=0,
                    speed=1.0,
                    playlists=playlists,
                    playlist_index=0,
                    source_groups=source_groups,
                    source_group_index=0,
                    source_index=0,
                )
                current_item = session_or_item
            else:
                session = session_or_item
                current_item = item
            return self._resolve_play_item(session, current_item)

        def detail_action_runner(item: PlayItem, action_id: str) -> list[PlaybackDetailAction]:
            playlist_index = 0
            for current_index, current_playlist in enumerate(playlists):
                if item in current_playlist:
                    playlist_index = current_index
                    break
            return self._run_detail_action(detail, playlists, playlist_index, item, action_id)

        metadata_hydrator = None
        is_short_drama = _is_short_drama_vod(detail) or (
            "短剧" in self._plugin_name
            and any(marker in self._plugin_name for marker in ("目录", "合集", "更新"))
        )
        if self._metadata_hydrator_factory is not None and self._danmaku_enabled and not is_short_drama:
            metadata_hydrator = self._metadata_hydrator_factory(
                source_kind="plugin",
                source_key=self._plugin_name,
                vod=detail,
                raw_detail=raw_detail,
            )
        metadata_scrape_service = None
        if self._metadata_scrape_service_factory is not None and self._danmaku_enabled and not is_short_drama:
            metadata_scrape_service = self._metadata_scrape_service_factory(
                source_kind="plugin",
                source_key=self._plugin_name,
                vod=detail,
                raw_detail=raw_detail,
            )
        episode_title_enhancer = None
        if self._episode_title_enhancer_factory is not None and self._danmaku_enabled and not is_short_drama:
            episode_title_enhancer = self._episode_title_enhancer_factory(
                source_kind="plugin",
                source_key=self._plugin_name,
                vod=detail,
                raw_detail=raw_detail,
            )

        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            playlists=playlists,
            playlist_index=0,
            source_groups=source_groups,
            source_group_index=0,
            source_index=0,
            clicked_index=0,
            source_kind="plugin",
            source_mode="detail",
            source_vod_id=source_vod_id,
            use_local_history=False,
            playback_loader=playback_loader,
            async_playback_loader=True,
            detail_action_runner=detail_action_runner,
            metadata_hydrator=metadata_hydrator,
            metadata_scrape_service=metadata_scrape_service,
            episode_title_enhancer=episode_title_enhancer,
            danmaku_controller=(
                self
                if self._danmaku_service is not None
                and (self._danmaku_enabled or self._danmaku_content_supported)
                and (not is_short_drama or self._danmaku_content_supported)
                else None
            ),
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )
