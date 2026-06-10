from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from time import monotonic
from urllib.parse import parse_qs, urlparse

from atv_player.api import ApiError
from atv_player.controllers.pagination import PageInfo, page_count_from_total
from atv_player.controllers.youtube_category_config import (
    normalize_youtube_vod_id,
    plan_youtube_query,
)
from atv_player.models import (
    AppConfig,
    CategoryFilter,
    CategoryFilterOption,
    DoubanCategory,
    OpenPlayerRequest,
    PlaybackDetailField,
    PlaybackDetailFieldAction,
    PlaybackDetailValuePart,
    PlaybackLoadResult,
    PlayItem,
    VodItem,
)

logger = logging.getLogger(__name__)


class _FlatEntries(list):
    def __init__(self, entries: list[dict], total: int | None = None) -> None:
        super().__init__(entries)
        self.total = total


def _page_count_for_loaded_youtube_page(page_number: int, item_count: int) -> int:
    normalized_page = max(1, int(page_number or 1))
    if item_count >= 30:
        return normalized_page + 1
    return normalized_page if item_count > 0 else 0


def _optional_total(value: object) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _page_info_for_loaded_youtube_page(
    page_number: int,
    item_count: int,
    *,
    total: int | None = None,
) -> int:
    page_count = _page_count_for_loaded_youtube_page(page_number, item_count)
    if total is None:
        return page_count
    return PageInfo(page_count, total)

_DEFAULT_CATEGORIES = [
    {"id": "cat_recommend", "name": "首页推荐", "query": "推荐", "order": 1},
    {"id": "cat_trending", "name": "热门", "query": "热门", "order": 2},
    {"id": "cat_recent", "name": "最近更新", "query": "最新", "order": 3},
    {"id": "cat_live", "name": "直播", "query": "直播", "order": 4},
    {"id": "cat_news", "name": "新闻", "query": "新闻", "order": 5},
    {"id": "cat_cdrama", "name": "中国电视", "query": "中国电视", "playlistOnly": True, "order": 6},
    {"id": "cat_drama", "name": "剧集", "query": "剧集", "playlistOnly": True, "order": 7},
    {"id": "cat_movie", "name": "中国电影", "query": "最新电影", "order": 8},
    {"id": "cat_movie_zh", "name": "电影", "query": "电影", "order": 9},
    {"id": "cat_movie_en", "name": "movie", "query": "movie", "order": 10},
    {"id": "cat_variety", "name": "中国综艺", "query": "喜剧综艺", "playlistOnly": True, "order": 11},
    {"id": "cat_variety_zh", "name": "综艺", "query": "综艺", "playlistOnly": True, "order": 12},
    {"id": "cat_anime", "name": "中国动漫", "query": "中国动漫", "playlistOnly": True, "order": 13},
    {"id": "cat_doc", "name": "中国纪录", "query": "中国纪录", "playlistOnly": True, "order": 14},
    {"id": "cat_doc_zh", "name": "纪录片", "query": "纪录片", "playlistOnly": True, "order": 15},
    {"id": "cat_doc_en", "name": "documentary", "query": "documentary", "playlistOnly": True, "order": 16},
    {"id": "cat_doc_bbc", "name": "BBC纪录", "query": "bbc documentary", "playlistOnly": True, "order": 17},
    {"id": "cat_music", "name": "中国音乐", "query": "华语音乐", "order": 18},
    {"id": "cat_music_zh", "name": "音乐", "query": "音乐", "order": 19},
    {"id": "cat_music_en", "name": "music", "query": "music", "order": 20},
    {"id": "cat_short", "name": "中国短剧", "query": "中国短剧", "playlistOnly": True, "order": 21},
    {"id": "cat_shorts", "name": "Shorts", "query": "#shorts", "order": 22},
    {"id": "cat_sports", "name": "体育", "query": "体育", "order": 23},
    {"id": "cat_animal", "name": "动物", "query": "动物世界", "order": 24},
    {"id": "cat_scenery", "name": "风光", "query": "风光", "order": 25},
    {"id": "cat_relax", "name": "放松", "query": "放松", "order": 26},
    {"id": "cat_4k", "name": "4K", "query": "4K", "order": 27},
    {"id": "cat_hdr", "name": "HDR", "query": "HDR", "order": 28},
]

_DEFAULT_FILTERS = [
    {"id": "filter_action", "name": "动作片", "categoryId": "cat_movie", "query": "动作电影", "order": 1},
    {"id": "filter_scifi", "name": "科幻片", "categoryId": "cat_movie", "query": "科幻电影", "order": 2},
    {"id": "filter_romance", "name": "爱情片", "categoryId": "cat_movie", "query": "爱情电影", "order": 3},
    {"id": "filter_comedy", "name": "喜剧片", "categoryId": "cat_movie", "query": "喜剧电影", "order": 4},
    {"id": "filter_horror", "name": "恐怖片", "categoryId": "cat_movie", "query": "恐怖电影", "order": 5},
    {"id": "filter_thriller", "name": "悬疑片", "categoryId": "cat_movie", "query": "悬疑电影", "order": 6},
    {"id": "filter_doc_cctv", "name": "CCTV纪录", "categoryId": "cat_doc", "query": "CCTV纪录", "order": 1},
    {"id": "filter_doc_natgeo", "name": "国家地理", "categoryId": "cat_doc", "query": "国家地理", "order": 2},
]

_LOGIN_CATEGORIES = [
    {"id": "cat_sub_feed", "name": "我的订阅视频"},
    {"id": "cat_sub_channels", "name": "我的订阅频道"},
    {"id": "cat_history", "name": "播放历史"},
    {"id": "cat_watch_later", "name": "稍后再看"},
]

_LOGIN_FEED_URLS = {
    "cat_sub_feed": ":ytsubs",
    "cat_sub_channels": "https://www.youtube.com/feed/channels",
    "cat_history": ":ythis",
    "cat_watch_later": ":ytwatchlater",
}
_YOUTUBE_LIST_CACHE_TTL_SECONDS = 30 * 60.0
_LOGIN_LIST_CACHE_TTL_SECONDS = _YOUTUBE_LIST_CACHE_TTL_SECONDS
_CHANNEL_CACHE_TTL_SECONDS = _YOUTUBE_LIST_CACHE_TTL_SECONDS


def _clone_vod_item(item: VodItem) -> VodItem:
    return replace(
        item,
        poster_candidates=list(item.poster_candidates),
        detail_fields=list(item.detail_fields),
        metadata_field_sources=dict(item.metadata_field_sources),
        items=list(item.items),
    )


def _clone_play_item(item: PlayItem) -> PlayItem:
    return replace(
        item,
        detail_actions=list(item.detail_actions),
        detail_fields=list(item.detail_fields),
        headers=dict(item.headers),
        audio_tracks=list(item.audio_tracks),
        external_subtitles=list(item.external_subtitles),
        playback_qualities=list(item.playback_qualities),
        danmaku_candidates=list(item.danmaku_candidates),
    )


def _clone_play_items(items: list[PlayItem]) -> list[PlayItem]:
    return [_clone_play_item(item) for item in items]


def _filters_for_category_id(category_id: str) -> list[CategoryFilter]:
    options = [
        CategoryFilterOption(name=str(item["name"]), value=str(item["id"]))
        for item in sorted(_DEFAULT_FILTERS, key=lambda item: int(item["order"]))
        if item["categoryId"] == category_id
    ]
    return [CategoryFilter(key="filter", name="筛选", options=options)] if options else []


def default_youtube_categories() -> list[DoubanCategory]:
    return [
        DoubanCategory(
            type_id=str(item["id"]),
            type_name=str(item["name"]),
            filters=_filters_for_category_id(str(item["id"])),
        )
        for item in sorted(_DEFAULT_CATEGORIES, key=lambda item: int(item["order"]))
    ]


def _youtube_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _youtube_video_thumbnail(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""


def _normalize_image_url(url: str) -> str:
    value = str(url or "").strip()
    return f"https:{value}" if value.startswith("//") else value


def _entry_url(entry: dict) -> str:
    value = str(entry.get("webpage_url") or entry.get("url") or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    entry_id = str(entry.get("id") or "").strip()
    ie_key = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
    if ie_key == "youtube" and entry_id:
        return _youtube_video_url(entry_id)
    if value and len(value) == 11:
        return _youtube_video_url(value)
    return value


def _video_id(entry: dict, url: str) -> str:
    entry_id = str(entry.get("id") or "").strip()
    ie_key = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
    if ie_key == "youtube" and entry_id:
        return entry_id
    parsed = urlparse(url)
    query_id = parse_qs(parsed.query).get("v", [""])[0]
    if query_id:
        return query_id
    if parsed.hostname == "youtu.be":
        return parsed.path.strip("/")
    return entry_id if len(entry_id) == 11 else ""


def _playlist_id(entry: dict, url: str) -> str:
    parsed = urlparse(url)
    query_id = parse_qs(parsed.query).get("list", [""])[0]
    if query_id:
        return query_id
    entry_id = str(entry.get("id") or "").strip()
    return entry_id if entry_id.startswith(("PL", "RD", "OLAK")) else ""


def _channel_id(entry: dict, url: str) -> str:
    entry_id = str(entry.get("channel_id") or entry.get("id") or "").strip()
    if entry_id.startswith("UC"):
        return entry_id
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "channel" and parts[1].startswith("UC"):
        return parts[1]
    ie_key = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
    if "youtubetab" in ie_key and url:
        return url.rstrip("/")
    return ""


def _entry_title(entry: dict, fallback: str = "") -> str:
    return str(entry.get("title") or entry.get("fulltitle") or fallback).strip()


def _entry_thumbnail(entry: dict, video_id: str = "") -> str:
    value = str(entry.get("thumbnail") or "").strip()
    if value:
        return _normalize_image_url(value)
    thumbnails = entry.get("thumbnails")
    if isinstance(thumbnails, list):
        avatar_candidates = []
        square_candidates = []
        fallback_candidates = []
        for thumbnail in reversed(thumbnails):
            if not isinstance(thumbnail, dict):
                continue
            value = str(thumbnail.get("url") or "").strip()
            if not value:
                continue
            value = _normalize_image_url(value)
            thumbnail_id = str(thumbnail.get("id") or "").lower()
            if "avatar" in thumbnail_id:
                avatar_candidates.append(value)
                continue
            try:
                width = int(thumbnail.get("width") or 0)
                height = int(thumbnail.get("height") or 0)
            except (TypeError, ValueError):
                width = 0
                height = 0
            if width > 0 and height > 0 and abs(width - height) <= max(2, width // 20):
                square_candidates.append(value)
                continue
            fallback_candidates.append(value)
        if avatar_candidates:
            return avatar_candidates[0]
        if square_candidates:
            return square_candidates[0]
        if fallback_candidates:
            return fallback_candidates[0]
    return _youtube_video_thumbnail(video_id)


def _entry_remarks(entry: dict) -> str:
    channel = str(entry.get("channel") or entry.get("uploader") or "").strip()
    duration = _format_detail_duration(entry.get("duration_string") or entry.get("duration"))
    return " | ".join(part for part in (channel, duration) if part)


def _format_detail_stat_value(raw_value: object) -> str:
    if isinstance(raw_value, bool):
        return str(raw_value)
    if isinstance(raw_value, int | float):
        numeric_value = float(raw_value)
        if numeric_value >= 10000:
            return f"{numeric_value / 10000:.1f}万"
        if numeric_value.is_integer():
            return str(int(numeric_value))
        return str(raw_value)
    value = str(raw_value or "").strip()
    if not value:
        return ""
    try:
        numeric_value = float(value)
    except ValueError:
        return value
    if numeric_value >= 10000:
        return f"{numeric_value / 10000:.1f}万"
    if numeric_value.is_integer():
        return str(int(numeric_value))
    return value


def _format_detail_date(raw_value: object) -> str:
    value = str(raw_value or "").strip()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return value


def _format_detail_duration(raw_value: object) -> str:
    value = str(raw_value or "").strip()
    if value and ":" in value:
        return value
    try:
        total_seconds = int(raw_value or 0)
    except (TypeError, ValueError):
        return ""
    if total_seconds <= 0:
        return ""
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _is_youtube_page_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    hostname = (parsed.hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname in {"youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"}


def _youtube_video_vod_id(video_id: str) -> str:
    return f"yt:video:{video_id}"


def _youtube_channel_vod_id(channel_ref: str) -> str:
    return f"yt:channel:{channel_ref}"


def _youtube_playlist_vod_id(playlist_id: str) -> str:
    return f"yt:playlist:{playlist_id}"


def _youtube_video_id_from_vod_id(vod_id: str) -> str:
    normalized = normalize_youtube_vod_id(vod_id)
    if normalized.startswith("yt:video:"):
        return normalized.removeprefix("yt:video:").strip()
    return normalized


def _youtube_channel_ref_from_vod_id(vod_id: str) -> str:
    normalized = normalize_youtube_vod_id(vod_id)
    if normalized.startswith("yt:channel:"):
        return normalized.removeprefix("yt:channel:").strip()
    return normalized


def _youtube_playlist_id_from_vod_id(vod_id: str) -> str:
    normalized = normalize_youtube_vod_id(vod_id)
    if normalized.startswith("yt:playlist:"):
        return normalized.removeprefix("yt:playlist:").strip()
    return normalized


def _format_detail_list(raw_value: object, *, limit: int = 8) -> str:
    if isinstance(raw_value, (list, tuple)):
        parts = [str(item or "").strip() for item in raw_value]
        parts = [part for part in parts if part]
        if limit > 0:
            parts = parts[:limit]
        return " / ".join(parts)
    return str(raw_value or "").strip()


def _append_detail_field(fields: list[PlaybackDetailField], label: str, value: object) -> None:
    normalized = str(value or "").strip()
    if not normalized:
        return
    fields.append(PlaybackDetailField(label, normalized))


def _append_youtube_vid_field(fields: list[PlaybackDetailField], video_id: str) -> None:
    normalized = str(video_id or "").strip()
    if not normalized:
        return
    if any(str(field.label).strip().upper() == "VID" for field in fields):
        return
    fields.append(
        PlaybackDetailField(
            label="VID",
            value_parts=[
                PlaybackDetailValuePart(
                    label=normalized,
                    action=PlaybackDetailFieldAction(
                        type="link",
                        value=_youtube_video_url(normalized),
                    ),
                )
            ],
        )
    )


def _missing_pic_count(items: list[VodItem]) -> int:
    return sum(1 for item in items if not item.vod_pic)


def _first_pic_sample(items: list[VodItem]) -> str:
    for item in items:
        if item.vod_pic:
            return item.vod_pic[:160]
    return ""


def _video_detail_fields(entry: dict) -> list[PlaybackDetailField]:
    fields: list[PlaybackDetailField] = []
    entry_url = _entry_url(entry)
    _append_youtube_vid_field(fields, _video_id(entry, entry_url))
    _append_detail_field(fields, "频道", entry.get("channel") or entry.get("uploader") or entry.get("creator"))
    _append_detail_field(fields, "发布", _format_detail_date(entry.get("upload_date") or entry.get("release_date")))
    _append_detail_field(fields, "时长", _format_detail_duration(entry.get("duration_string") or entry.get("duration")))
    _append_detail_field(fields, "播放", _format_detail_stat_value(entry.get("view_count")))
    _append_detail_field(fields, "点赞", _format_detail_stat_value(entry.get("like_count")))
    _append_detail_field(fields, "评论", _format_detail_stat_value(entry.get("comment_count")))
    _append_detail_field(fields, "分类", _format_detail_list(entry.get("categories"), limit=3))
    _append_detail_field(fields, "标签", _format_detail_list(entry.get("tags"), limit=8))
    _append_detail_field(fields, "简介", entry.get("description"))
    return fields


def _detail_fields_with_video_id(
    existing_fields: list[PlaybackDetailField],
    video_id: str,
) -> list[PlaybackDetailField]:
    fields = list(existing_fields)
    _append_youtube_vid_field(fields, video_id)
    return fields


class YouTubeController:
    supports_search = True
    uses_page_count_for_pagination = True

    def __init__(
        self,
        config: AppConfig,
        *,
        yt_dlp_service,
        playback_history_loader: Callable[[str], object | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
        category_config_loader: Callable[[], list[DoubanCategory]] | None = None,
        now: Callable[[], float] = monotonic,
    ) -> None:
        self._config = config
        self._yt_dlp_service = yt_dlp_service
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver
        self._category_config_loader = category_config_loader
        self._now = now
        self._channel_thumbnail_cache: dict[str, tuple[float, str]] = {}
        self._channel_playlist_cache: dict[
            str,
            tuple[float, str, list[PlayItem]],
        ] = {}
        self._login_list_cache: dict[str, tuple[float, list[VodItem], int]] = {}

    def _has_cookie_browser(self) -> bool:
        return bool(str(getattr(self._config, "youtube_cookie_browser", "") or "").strip())

    def _login_list_cache_key(self, category_id: str, page_number: int) -> str:
        return f"{category_id}:{page_number}"

    def _get_cached_login_list(self, key: str) -> tuple[list[VodItem], int] | None:
        cached = self._login_list_cache.get(key)
        if cached is None:
            return None
        expires_at, items, total = cached
        if expires_at <= self._now():
            self._login_list_cache.pop(key, None)
            return None
        return [_clone_vod_item(item) for item in items], total

    def _store_login_list_cache(self, key: str, items: list[VodItem], total: int) -> None:
        self._login_list_cache[key] = (
            self._now() + _LOGIN_LIST_CACHE_TTL_SECONDS,
            [_clone_vod_item(item) for item in items],
            total,
        )

    def _flat_entries(self, url: str, page: int, page_size: int = 30) -> list[dict]:
        service = self._yt_dlp_service
        if service is None or not service.is_available():
            logger.info("YouTube yt-dlp list skipped url=%s reason=unavailable", url)
            return []
        extract = getattr(service, "extract_flat_playlist", None)
        if not callable(extract):
            logger.info("YouTube yt-dlp list skipped url=%s reason=no_extract_flat_playlist", url)
            return []
        started_at = monotonic()
        try:
            extracted = extract(url, page=page, page_size=page_size)
            entries = _FlatEntries(
                list(extracted or []),
                _optional_total(getattr(extracted, "total", None)),
            )
            logger.info(
                "YouTube yt-dlp list loaded url=%s page=%s page_size=%s entries=%s total=%s elapsed=%.3fs",
                url,
                page,
                page_size,
                len(entries),
                entries.total,
                monotonic() - started_at,
            )
            return entries
        except ApiError:
            raise
        except Exception as exc:
            logger.warning(
                "YouTube yt-dlp list failed url=%s page=%s page_size=%s elapsed=%.3fs error=%s",
                url,
                page,
                page_size,
                monotonic() - started_at,
                exc,
            )
            raise ApiError(f"YouTube 列表加载失败: {exc}") from exc

    def load_categories(self) -> list[DoubanCategory]:
        if self._category_config_loader is not None:
            categories = [replace(category) for category in self._category_config_loader()]
            if not categories:
                categories = default_youtube_categories()
        else:
            categories = default_youtube_categories()
        if self._has_cookie_browser():
            categories = [
                DoubanCategory(type_id=str(item["id"]), type_name=str(item["name"]))
                for item in _LOGIN_CATEGORIES
            ] + categories
        return categories

    def _filters_for_category(self, category_id: str) -> list[CategoryFilter]:
        return _filters_for_category_id(category_id)

    def _map_entry(self, entry: dict) -> VodItem | None:
        url = _entry_url(entry)
        video_id = _video_id(entry, url)
        playlist_id = _playlist_id(entry, url)
        channel_id = _channel_id(entry, url)
        if channel_id and not video_id and not playlist_id:
            return VodItem(
                vod_id=_youtube_channel_vod_id(channel_id),
                vod_name=_entry_title(entry, channel_id),
                vod_pic=_entry_thumbnail(entry),
                vod_remarks="频道",
                vod_tag="file",
            )
        if playlist_id and not video_id:
            return VodItem(
                vod_id=_youtube_playlist_vod_id(playlist_id),
                vod_name=_entry_title(entry, playlist_id),
                vod_pic=_entry_thumbnail(entry),
                vod_remarks="Playlist",
                vod_tag="file",
            )
        if video_id:
            return VodItem(
                vod_id=_youtube_video_vod_id(video_id),
                vod_name=_entry_title(entry, video_id),
                vod_pic=_entry_thumbnail(entry, video_id),
                vod_remarks=_entry_remarks(entry),
                vod_tag="file",
            )
        return None

    def _map_entries(self, entries: list[dict], *, channels_only: bool = False, videos_only: bool = False) -> list[VodItem]:
        items = []
        seen = set()
        for entry in entries:
            item = self._map_entry(entry)
            if item is None:
                continue
            if channels_only and not item.vod_id.startswith("yt:channel:"):
                continue
            if videos_only and item.vod_id.startswith(("yt:channel:", "yt:playlist:")):
                continue
            if item.vod_id in seen:
                continue
            seen.add(item.vod_id)
            items.append(item)
        return items

    def _channel_ref_from_vod_id(self, vod_id: str) -> str:
        if not vod_id.startswith("yt:channel:"):
            return ""
        return vod_id.removeprefix("yt:channel:").strip()

    def _channel_metadata_url(self, channel_ref: str) -> str:
        if channel_ref.startswith(("http://", "https://")):
            return channel_ref.rstrip("/")
        if channel_ref.startswith("@"):
            return f"https://www.youtube.com/{channel_ref}"
        if channel_ref.startswith("UC"):
            return f"https://www.youtube.com/channel/{channel_ref}"
        return channel_ref

    def _load_channel_thumbnail(self, channel_ref: str) -> str:
        metadata_url = self._channel_metadata_url(channel_ref)
        if not metadata_url:
            return ""
        cached = self._channel_thumbnail_cache.get(metadata_url)
        if cached is not None:
            expires_at, cached_thumbnail = cached
            if expires_at > self._now():
                return cached_thumbnail
            self._channel_thumbnail_cache.pop(metadata_url, None)
        thumbnail = ""
        try:
            entries = self._flat_entries(metadata_url, 1, 1)
        except Exception:
            entries = []
        for entry in entries:
            thumbnail = _entry_thumbnail(entry)
            if thumbnail:
                break
        self._channel_thumbnail_cache[metadata_url] = (
            self._now() + _CHANNEL_CACHE_TTL_SECONDS,
            thumbnail,
        )
        return thumbnail

    def _enrich_channel_thumbnails(self, items: list[VodItem]) -> list[VodItem]:
        enriched = []
        for item in items:
            if item.vod_pic or not item.vod_id.startswith("yt:channel:"):
                enriched.append(item)
                continue
            thumbnail = self._load_channel_thumbnail(self._channel_ref_from_vod_id(item.vod_id))
            if thumbnail:
                item = VodItem(
                    vod_id=item.vod_id,
                    vod_name=item.vod_name,
                    detail_style=item.detail_style,
                    path=item.path,
                    share_type=item.share_type,
                    vod_pic=thumbnail,
                    poster_candidates=list(item.poster_candidates),
                    vod_tag=item.vod_tag,
                    vod_time=item.vod_time,
                    vod_remarks=item.vod_remarks,
                    vod_play_from=item.vod_play_from,
                    vod_play_url=item.vod_play_url,
                    type_name=item.type_name,
                    category_name=item.category_name,
                    vod_content=item.vod_content,
                    vod_year=item.vod_year,
                    vod_area=item.vod_area,
                    vod_lang=item.vod_lang,
                    vod_director=item.vod_director,
                    vod_actor=item.vod_actor,
                    epg_current=item.epg_current,
                    epg_schedule=item.epg_schedule,
                    dbid=item.dbid,
                    type=item.type,
                    detail_fields=list(item.detail_fields),
                    items=list(item.items),
                )
            enriched.append(item)
        return enriched

    def _resolve_category_query(self, category_id: str, filters: dict[str, str]) -> tuple[str, bool]:
        category = next((item for item in _DEFAULT_CATEGORIES if item["id"] == category_id), None)
        if category is None:
            return "", False
        query = str(category["query"])
        playlist_only = bool(category.get("playlistOnly", False))
        filter_id = str((filters or {}).get("filter") or "")
        selected = next(
            (
                item for item in _DEFAULT_FILTERS
                if item["id"] == filter_id and item["categoryId"] == category_id
            ),
            None,
        )
        if selected is not None:
            query = str(selected["query"])
        return query, playlist_only

    def load_items(
        self,
        category_id: str,
        page: int,
        filters: dict[str, str] | None = None,
    ) -> tuple[list[VodItem], int]:
        page_number = max(1, int(page or 1))
        if category_id in _LOGIN_FEED_URLS:
            if not self._has_cookie_browser():
                return [], 0
            cache_key = self._login_list_cache_key(category_id, page_number)
            cached = self._get_cached_login_list(cache_key)
            if cached is not None:
                items, total = cached
                logger.info(
                    "YouTube login list cache hit category=%s page=%s items=%s missing_pic=%s sample_pic=%s",
                    category_id,
                    page_number,
                    len(items),
                    _missing_pic_count(items),
                    _first_pic_sample(items),
                )
                return cached
            entries = self._flat_entries(_LOGIN_FEED_URLS[category_id], page_number)
            items = self._map_entries(
                entries,
                channels_only=category_id == "cat_sub_channels",
                videos_only=category_id in {"cat_sub_feed", "cat_history", "cat_watch_later"},
            )
            if category_id == "cat_sub_channels":
                items = self._enrich_channel_thumbnails(items)
            page_count = _page_info_for_loaded_youtube_page(
                page_number,
                len(items),
                total=_optional_total(getattr(entries, "total", None)),
            )
            if items:
                self._store_login_list_cache(cache_key, items, page_count)
            logger.info(
                "YouTube login list loaded via yt-dlp category=%s page=%s items=%s missing_pic=%s total=%s sample_pic=%s",
                category_id,
                page_number,
                len(items),
                _missing_pic_count(items),
                page_count,
                _first_pic_sample(items),
            )
            return items, page_count
        default_category_ids = {str(item["id"]) for item in _DEFAULT_CATEGORIES}
        if category_id in default_category_ids:
            query, playlist_only = self._resolve_category_query(category_id, filters or {})
            if not query:
                return [], 0
            if playlist_only:
                query = f"{query} playlist"
        else:
            plan = plan_youtube_query(category_id, filters or {})
            if plan.unsupported_filters:
                logger.debug("YouTube unsupported search filters ignored: %s", plan.unsupported_filters)
            if not plan.value:
                return [], 0
            if plan.kind == "channel":
                request = self._build_channel_request(plan.value, _youtube_channel_vod_id(plan.value))
                items = [
                    VodItem(
                        vod_id=item.vod_id,
                        vod_name=item.title,
                        vod_pic=item.video_cover_override,
                        vod_tag="file",
                    )
                    for item in request.playlist
                ]
                return items, page_count_from_total(len(items))
            if plan.kind == "playlist":
                request = self._build_playlist_request(plan.value, _youtube_playlist_vod_id(plan.value))
                items = [
                    VodItem(
                        vod_id=item.vod_id,
                        vod_name=item.title,
                        vod_pic=item.video_cover_override,
                        vod_tag="file",
                    )
                    for item in request.playlist
                ]
                return items, page_count_from_total(len(items))
            query = plan.value
        entries = self._flat_entries(f"ytsearchall:{query}", page_number)
        items = self._map_entries(entries)
        logger.info(
            "YouTube category list loaded category=%s page=%s query=%s items=%s missing_pic=%s sample_pic=%s",
            category_id,
            page_number,
            query,
            len(items),
            _missing_pic_count(items),
            _first_pic_sample(items),
        )
        page_count = _page_info_for_loaded_youtube_page(
            page_number,
            len(items),
            total=_optional_total(getattr(entries, "total", None)),
        )
        return items, page_count

    def search_items(
        self,
        keyword: str,
        page: int,
        category_id: str = "",
    ) -> tuple[list[VodItem], int]:
        del category_id
        page_number = max(1, int(page or 1))
        query = str(keyword or "").strip()
        if not query:
            return [], 0
        entries = self._flat_entries(f"ytsearchall:{query}", page_number)
        items = self._map_entries(entries)
        logger.info(
            "YouTube search loaded keyword=%s page=%s items=%s missing_pic=%s sample_pic=%s",
            query,
            page_number,
            len(items),
            _missing_pic_count(items),
            _first_pic_sample(items),
        )
        page_count = _page_info_for_loaded_youtube_page(
            page_number,
            len(items),
            total=_optional_total(getattr(entries, "total", None)),
        )
        return items, page_count

    def _entry_for_video(self, video_id: str) -> dict:
        entries = self._flat_entries(_youtube_video_url(video_id), 1, 1)
        for entry in entries:
            if _video_id(entry, _entry_url(entry)) == video_id or str(entry.get("id") or "") == video_id:
                return entry
        return {"id": video_id, "title": "YouTube视频", "url": _youtube_video_url(video_id), "ie_key": "Youtube"}

    def _play_item_from_entry(self, entry: dict, *, media_title: str = "", playlist_id: str = "") -> PlayItem | None:
        url = _entry_url(entry)
        video_id = _video_id(entry, url)
        if not video_id:
            return None
        title = _entry_title(entry, video_id)
        playback_url = _youtube_video_url(video_id)
        return PlayItem(
            title=title,
            url=playback_url,
            original_url=playback_url,
            vod_id=_youtube_video_vod_id(video_id),
            media_title=media_title,
            video_cover_override=_entry_thumbnail(entry, video_id),
            play_source="YouTube",
            detail_fields=_video_detail_fields(entry),
            ytdl_format=self._yt_dlp_service.playback_format_selector(),
        )

    def _channel_videos_url(self, channel_ref: str) -> str:
        return (
            f"{channel_ref.rstrip('/')}/videos"
            if channel_ref.startswith(("http://", "https://"))
            else f"https://www.youtube.com/{channel_ref}/videos"
            if channel_ref.startswith("@")
            else f"https://www.youtube.com/channel/{channel_ref}/videos"
        )

    def _get_cached_channel_playlist(
        self,
        channel_ref: str,
    ) -> tuple[str, list[PlayItem]] | None:
        cache_key = self._channel_videos_url(channel_ref)
        cached = self._channel_playlist_cache.get(cache_key)
        if cached is None:
            return None
        expires_at, channel_title, playlist = cached
        if expires_at <= self._now():
            self._channel_playlist_cache.pop(cache_key, None)
            return None
        return channel_title, _clone_play_items(playlist)

    def _store_channel_playlist_cache(
        self,
        channel_ref: str,
        channel_title: str,
        playlist: list[PlayItem],
    ) -> None:
        cache_key = self._channel_videos_url(channel_ref)
        self._channel_playlist_cache[cache_key] = (
            self._now() + _CHANNEL_CACHE_TTL_SECONDS,
            channel_title,
            _clone_play_items(playlist),
        )

    def _load_channel_playlist(self, channel_ref: str) -> tuple[str, list[PlayItem]]:
        cached = self._get_cached_channel_playlist(channel_ref)
        if cached is not None:
            channel_title, playlist = cached
            logger.info(
                "YouTube channel playlist cache hit channel=%s items=%s",
                channel_ref,
                len(playlist),
            )
            return channel_title, playlist
        entries = self._flat_entries(self._channel_videos_url(channel_ref), 1, 200)
        channel_title, playlist = self._channel_playlist_from_entries(
            channel_ref,
            entries,
        )
        self._store_channel_playlist_cache(channel_ref, channel_title, playlist)
        return channel_title, playlist

    def _channel_playlist_from_entries(
        self,
        channel_ref: str,
        entries: list[dict],
    ) -> tuple[str, list[PlayItem]]:
        channel_title = channel_ref
        for entry in entries:
            title = str(entry.get("channel") or entry.get("uploader") or "").strip()
            if title:
                channel_title = title
                break
        playlist = []
        for entry in entries:
            item = self._play_item_from_entry(entry, media_title=channel_title)
            if item is not None:
                playlist.append(item)
        return channel_title, playlist

    def _build_video_request(self, video_id: str, source_vod_id: str) -> OpenPlayerRequest:
        entry = self._entry_for_video(video_id)
        title = _entry_title(entry, "YouTube视频")
        thumb = _entry_thumbnail(entry, video_id)
        vod = VodItem(
            vod_id=video_id,
            vod_name=title,
            detail_style="youtube",
            vod_pic=thumb,
            vod_remarks=_entry_remarks(entry),
            detail_fields=_video_detail_fields(entry),
        )
        item = self._play_item_from_entry(entry, media_title=title)
        if item is None:
            playback_url = _youtube_video_url(video_id)
            item = PlayItem(
                title=title,
                url=playback_url,
                original_url=playback_url,
                vod_id=_youtube_video_vod_id(video_id),
                media_title=title,
                video_cover_override=thumb,
                play_source="YouTube",
                ytdl_format=self._yt_dlp_service.playback_format_selector(),
            )
        return self._request(vod, [item], source_vod_id)

    def _build_fast_video_request(
        self,
        video_id: str,
        source_vod_id: str,
        source_item,
        *,
        playlist_id: str = "",
    ) -> OpenPlayerRequest:
        title = str(getattr(source_item, "vod_name", "") or video_id).strip() or video_id
        thumb = _normalize_image_url(str(getattr(source_item, "vod_pic", "") or "").strip())
        remarks = str(getattr(source_item, "vod_remarks", "") or "").strip()
        content = str(getattr(source_item, "vod_content", "") or "").strip()
        detail_fields = _detail_fields_with_video_id(
            list(getattr(source_item, "detail_fields", []) or []),
            video_id,
        )
        original_url = _youtube_video_url(video_id)
        if playlist_id:
            original_url = f"{original_url}&list={playlist_id}"
        vod = VodItem(
            vod_id=source_vod_id,
            vod_name=title,
            detail_style="youtube",
            vod_pic=thumb,
            vod_remarks=remarks,
            vod_content=content,
            type_name=str(getattr(source_item, "type_name", "") or ""),
            category_name=str(getattr(source_item, "category_name", "") or ""),
            detail_fields=detail_fields,
        )
        item = PlayItem(
            title=title,
            url=original_url,
            original_url=original_url,
            vod_id=_youtube_video_vod_id(video_id),
            media_title=title,
            video_cover_override=thumb,
            play_source="YouTube",
            detail_fields=detail_fields,
            ytdl_format=self._yt_dlp_service.playback_format_selector(),
        )
        return self._request(vod, [item], source_vod_id)

    def _build_fast_channel_request(
        self,
        channel_ref: str,
        source_vod_id: str,
        source_item,
    ) -> OpenPlayerRequest:
        title = (
            str(getattr(source_item, "vod_name", "") or channel_ref).strip()
            or channel_ref
        )
        thumb = _normalize_image_url(
            str(getattr(source_item, "vod_pic", "") or "").strip()
        )
        detail_fields = list(getattr(source_item, "detail_fields", []) or [])
        vod = VodItem(
            vod_id=source_vod_id,
            vod_name=title,
            detail_style="youtube",
            vod_pic=thumb,
            vod_remarks=str(
                getattr(source_item, "vod_remarks", "") or "频道"
            ).strip(),
            vod_content=str(getattr(source_item, "vod_content", "") or "").strip(),
            type_name=str(getattr(source_item, "type_name", "") or ""),
            category_name=str(getattr(source_item, "category_name", "") or ""),
            detail_fields=detail_fields,
        )
        item = PlayItem(
            title=title,
            url="",
            vod_id=source_vod_id,
            media_title=title,
            video_cover_override=thumb,
            play_source="YouTube",
            detail_fields=detail_fields,
        )
        return self._request(vod, [item], source_vod_id)

    def _build_playlist_request(self, playlist_id: str, source_vod_id: str) -> OpenPlayerRequest:
        entries = self._flat_entries(f"https://www.youtube.com/playlist?list={playlist_id}", 1, 200)
        playlist = [
            item for item in (self._play_item_from_entry(entry, media_title="YouTube播放列表", playlist_id=playlist_id) for entry in entries)
            if item is not None
        ]
        vod = VodItem(
            vod_id=_youtube_playlist_vod_id(playlist_id),
            vod_name="YouTube播放列表",
            detail_style="youtube",
            vod_remarks="播放列表",
            vod_pic=playlist[0].video_cover_override if playlist else "",
        )
        return self._request(vod, playlist, source_vod_id)

    def _build_channel_request(self, channel_ref: str, source_vod_id: str) -> OpenPlayerRequest:
        channel_title, playlist = self._load_channel_playlist(channel_ref)
        vod = VodItem(
            vod_id=_youtube_channel_vod_id(channel_ref),
            vod_name=channel_title,
            detail_style="youtube",
            vod_remarks="频道",
            vod_pic=playlist[0].video_cover_override if playlist else "",
        )
        return self._request(vod, playlist, source_vod_id)

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        raw = str(vod_id or "").strip()
        if raw.startswith("yt:entry:"):
            _prefix, _entry, playlist_id, video_id = raw.split(":", 3)
            return self._build_video_request(
                video_id,
                _youtube_playlist_vod_id(playlist_id) if playlist_id else _youtube_video_vod_id(video_id),
            )
        normalized = normalize_youtube_vod_id(raw)
        if normalized.startswith("yt:playlist:"):
            return self._build_playlist_request(_youtube_playlist_id_from_vod_id(normalized), normalized)
        if normalized.startswith("yt:channel:"):
            return self._build_channel_request(_youtube_channel_ref_from_vod_id(normalized), normalized)
        if normalized.startswith("yt:video:"):
            video_id = _youtube_video_id_from_vod_id(normalized)
            return self._build_video_request(video_id, normalized)
        if normalized.startswith("UC"):
            return self._build_channel_request(normalized, _youtube_channel_vod_id(normalized))
        if normalized.startswith("@"):
            return self._build_channel_request(normalized, _youtube_channel_vod_id(normalized))
        if normalized:
            return self._build_video_request(normalized, _youtube_video_vod_id(normalized))
        raise ValueError(f"没有可播放的项目: {vod_id}")

    def build_request_from_item(self, item) -> OpenPlayerRequest:
        raw = str(getattr(item, "vod_id", "") or "").strip()
        if raw.startswith("yt:entry:"):
            _prefix, _entry, playlist_id, video_id = raw.split(":", 3)
            source_vod_id = _youtube_playlist_vod_id(playlist_id) if playlist_id else _youtube_video_vod_id(video_id)
            return self._build_fast_video_request(
                video_id,
                source_vod_id,
                item,
                playlist_id=playlist_id,
            )
        normalized = normalize_youtube_vod_id(raw)
        if normalized.startswith("yt:channel:"):
            return self._build_fast_channel_request(
                _youtube_channel_ref_from_vod_id(normalized),
                normalized,
                item,
            )
        if normalized.startswith("yt:playlist:"):
            return self._build_playlist_request(_youtube_playlist_id_from_vod_id(normalized), normalized)
        if normalized.startswith("yt:video:"):
            return self._build_fast_video_request(_youtube_video_id_from_vod_id(normalized), normalized, item)
        if normalized.startswith("@"):
            return self._build_fast_channel_request(
                normalized,
                _youtube_channel_vod_id(normalized),
                item,
            )
        if normalized and not normalized.startswith("@"):
            return self._build_fast_video_request(normalized, _youtube_video_vod_id(normalized), item)
        return self.build_request(normalized)

    def _request(self, vod: VodItem, playlist: list[PlayItem], source_vod_id: str) -> OpenPlayerRequest:
        if not playlist:
            raise ValueError(f"没有可播放的项目: {vod.vod_name or source_vod_id}")
        source_vod_id = source_vod_id or vod.vod_id
        history_loader = None
        if self._playback_history_loader is not None:
            def history_loader(source_vod_id=source_vod_id):
                return self._playback_history_loader(source_vod_id)
        history_saver = None
        if self._playback_history_saver is not None:
            def history_saver(payload, source_vod_id=source_vod_id):
                return self._playback_history_saver(source_vod_id, payload)
        return OpenPlayerRequest(
            vod=vod,
            playlist=playlist,
            playlists=[playlist],
            clicked_index=0,
            source_kind="youtube",
            source_mode="detail",
            source_vod_id=source_vod_id,
            use_local_history=False,
            playback_loader=self._load_playback_item,
            async_playback_loader=True,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )

    def _playback_url(self, value: str) -> str:
        raw = str(value or "").strip()
        if raw.startswith("yt:entry:"):
            _prefix, _entry, playlist_id, video_id = raw.split(":", 3)
            return f"{_youtube_video_url(video_id)}&list={playlist_id}" if playlist_id else _youtube_video_url(video_id)
        normalized = normalize_youtube_vod_id(raw)
        if normalized.startswith(("http://", "https://")):
            return normalized
        if normalized.startswith(("yt:channel:", "yt:playlist:", "@")):
            return normalized
        return _youtube_video_url(_youtube_video_id_from_vod_id(normalized))

    def _should_resolve_with_full_metadata(self, session, current_item: PlayItem, current_url: str, source_url: str) -> bool:
        if current_url and current_url != source_url and not _is_youtube_page_url(current_url):
            return True
        if session is None:
            return False
        vod_id = str(getattr(session.vod, "vod_id", "") or "").strip()
        if vod_id.startswith(("yt:channel:", "yt:playlist:")):
            return True
        return len(getattr(session, "playlist", []) or []) > 1

    def _resolve_playback_result(
        self,
        service,
        source_url: str,
        *,
        selected_quality_id: str,
        selected_audio_track_id: str,
        current_url: str,
        use_full_metadata: bool,
    ):
        can_fast_resolve = (
            (not current_url or current_url == source_url)
            and not selected_audio_track_id
            and not selected_quality_id.startswith("ytdlp_")
            and not use_full_metadata
            and hasattr(service, "resolve_fast")
        )
        if can_fast_resolve:
            return service.resolve_fast(source_url)
        if selected_quality_id.startswith("ytdlp_"):
            resolver = (
                service.resolve_for_quality_full
                if use_full_metadata and hasattr(service, "resolve_for_quality_full")
                else service.resolve_for_quality
            )
            return resolver(
                source_url,
                selected_quality_id,
                audio_track_id=selected_audio_track_id,
            )
        if use_full_metadata and hasattr(service, "resolve_full"):
            return service.resolve_full(
                source_url,
                max_height=None,
                selected_audio_track_id=selected_audio_track_id,
            )
        return service.resolve(
            source_url,
            max_height=None,
            selected_audio_track_id=selected_audio_track_id,
        )

    def _load_playback_item(self, session_or_item, item: PlayItem | None = None):
        session = session_or_item if item is not None else None
        current_item = item or session_or_item
        current_vod_id = normalize_youtube_vod_id(str(current_item.vod_id or "").strip())
        if current_vod_id.startswith("yt:channel:") or current_vod_id.startswith("@"):
            return self._load_channel_playback_item(session, current_item)
        source_url = (current_item.original_url or self._playback_url(current_item.vod_id)).strip()
        service = self._yt_dlp_service
        if service is None or not service.is_available():
            raise ValueError("yt-dlp 不可用")
        selected_quality_id = current_item.selected_playback_quality_id or ""
        selected_audio_track_id = current_item.selected_audio_track_id or ""
        current_url = str(current_item.url or "").strip()
        result = self._resolve_playback_result(
            service,
            source_url,
            selected_quality_id=selected_quality_id,
            selected_audio_track_id=selected_audio_track_id,
            current_url=current_url,
            use_full_metadata=self._should_resolve_with_full_metadata(session, current_item, current_url, source_url),
        )
        service.apply_result(
            result,
            vod=None if session is None else session.vod,
            item=current_item,
            source_url=source_url,
        )
        return None

    def _load_channel_playback_item(
        self,
        session,
        current_item: PlayItem,
    ) -> PlaybackLoadResult:
        channel_vod_id = normalize_youtube_vod_id(str(current_item.vod_id or "").strip())
        channel_ref = _youtube_channel_ref_from_vod_id(channel_vod_id)
        channel_title, playlist = self._load_channel_playlist(channel_ref)
        if not playlist:
            raise ValueError(f"没有可播放的项目: {current_item.title or channel_ref}")
        if session is not None:
            session.vod.vod_name = channel_title
            session.vod.vod_remarks = "频道"
            if not session.vod.vod_pic:
                session.vod.vod_pic = playlist[0].video_cover_override
        start_index = 0
        if session is not None and self._playback_history_loader is not None:
            history = self._playback_history_loader(str(current_item.vod_id or ""))
            if history is not None and 0 <= int(history.episode) < len(playlist):
                start_index = int(history.episode)
        service = self._yt_dlp_service
        if service is None or not service.is_available():
            raise ValueError("yt-dlp 不可用")
        start_item = playlist[start_index]
        source_url = (start_item.original_url or self._playback_url(start_item.vod_id)).strip()
        selected_quality_id = start_item.selected_playback_quality_id or ""
        selected_audio_track_id = start_item.selected_audio_track_id or ""
        current_url = str(current_item.url or "").strip()
        result = self._resolve_playback_result(
            service,
            source_url,
            selected_quality_id=selected_quality_id,
            selected_audio_track_id=selected_audio_track_id,
            current_url=current_url,
            use_full_metadata=True,
        )
        service.apply_result(
            result,
            vod=None,
            item=start_item,
            source_url=source_url,
        )
        return PlaybackLoadResult(
            replacement_playlist=playlist,
            replacement_start_index=start_index,
        )
