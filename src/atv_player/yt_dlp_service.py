from __future__ import annotations

import base64
import html
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from time import monotonic
from typing import Callable
from urllib.parse import parse_qs, urlparse

from atv_player.models import (
    AppConfig,
    ExternalSubtitleOption,
    PlaybackDetailField,
    PlayItem,
    VideoQualityOption,
    VodItem,
    YtdlpAudioTrackOption,
)
from atv_player.network_proxy import ProxyDecider, build_ytdlp_proxy_args
from atv_player.player.ytdlp_runtime import (
    build_ytdlp_command_args,
    resolve_system_ytdlp_path,
)

logger = logging.getLogger(__name__)


class FlatPlaylistResult(list):
    def __init__(self, entries: list[dict], total: int | None = None) -> None:
        super().__init__(entries)
        self.total = total


def _optional_playlist_total(value: object) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _playlist_total_from_info(info: dict) -> int | None:
    for key in (
        "playlist_count",
        "n_entries",
        "total_entries",
        "entries_count",
        "playlist_n_entries",
    ):
        total = _optional_playlist_total(info.get(key))
        if total is not None:
            return total
    return None


_KNOWN_YTDLP_DOMAINS = frozenset({
    "youtube.com",
    "youtu.be",
    "m.youtube.com",
    "www.youtube.com",
    "music.youtube.com",
    "twitter.com",
    "x.com",
    "mobile.twitter.com",
    "instagram.com",
    "www.instagram.com",
    "tiktok.com",
    "www.tiktok.com",
    "vimeo.com",
    "www.vimeo.com",
    "dailymotion.com",
    "www.dailymotion.com",
    "twitch.tv",
    "www.twitch.tv",
    "facebook.com",
    "www.facebook.com",
    "bilibili.com",
    "www.bilibili.com",
    "b23.tv",
    "nicovideo.jp",
    "www.nicovideo.jp",
    "soundcloud.com",
    "streamable.com",
    "reddit.com",
    "www.reddit.com",
    "rumble.com",
    "odysee.com",
    "peertube.tv",
    "bandcamp.com",
    "pinterest.com",
    "tumblr.com",
    "weibo.com",
    "www.weibo.com",
    "m.weibo.com",
})

_LANG_CODE_NAMES: dict[str, str] = {
    "en": "英文",
    "zh-Hans": "简体中文",
    "zh-Hant": "繁体中文",
    "zh": "中文",
    "zh-CN": "中文",
    "zh-TW": "中文",
}

_DEFAULT_STARTUP_MAX_HEIGHT = 1080
_DASH_DATA_URI_PREFIX = "data:application/dash+xml;base64,"
_DASH_HTTP_CHUNK_SIZE_SCHEME = "urn:atv-player:http-chunk-size"
_DEFAULT_YOUTUBE_VIDEO_CODEC = "vp9"
_YOUTUBE_VIDEO_CODEC_VALUES = {"vp9", "av1", "auto"}
_DIRECT_SUBTITLE_FORMAT_RANK: dict[str, int] = {
    "vtt": 0,
    "srt": 1,
    "subrip": 1,
    "ass": 2,
    "ssa": 3,
}

_ENGLISH_AUDIO_PREFIXES = ("en", "eng", "en-")
_CHINESE_AUDIO_PREFIXES = ("zh", "chi", "zho", "cmn", "zh-", "zh_")
_YOUTUBE_METADATA_LANGUAGE_VALUES = {"zh-CN", "zh-TW", "zh-HK", "en"}
_YOUTUBE_REGION_VALUES = {"CN", "US", "JP", "SG", "HK", "TW"}
_YOUTUBE_AUDIO_LANGUAGE_VALUES = {"zh", "en"}
_YOUTUBE_SUBTITLE_LANGUAGE_VALUES = {"zh-CN", "zh-TW", "zh-HK", "en"}
_YOUTUBE_SUBTITLE_LANGUAGE_ALIASES: dict[str, set[str]] = {
    "zh-CN": {"zh-CN", "zh-Hans", "zh"},
    "zh-TW": {"zh-TW", "zh-Hant"},
    "zh-HK": {"zh-HK", "zh-Hant"},
    "en": {"en"},
}
_YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


def looks_like_youtube_video_id(value: str) -> bool:
    return bool(_YOUTUBE_VIDEO_ID_PATTERN.fullmatch(str(value or "").strip()))


def _canonicalize_ytdlp_url(url: str) -> str:
    candidate = str(url or "").strip()
    if not candidate:
        return ""
    if candidate.startswith("yt:video:"):
        video_id = candidate.removeprefix("yt:video:").strip()
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
    if looks_like_youtube_video_id(candidate):
        return f"https://www.youtube.com/watch?v={candidate}"
    return candidate


def _has_muxed_audio(fmt: dict) -> bool:
    acodec = fmt.get("acodec", "") or ""
    return acodec != "none"


def _quality_id_for_height(height: int) -> str:
    return f"ytdlp_{height}"


def _quality_height_from_id(quality_id: str) -> int | None:
    if not quality_id.startswith("ytdlp_"):
        return None
    suffix = quality_id.removeprefix("ytdlp_")
    if not suffix.isdigit():
        return None
    height = int(suffix)
    return height if height > 0 else None


def _normalize_youtube_video_codec(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _YOUTUBE_VIDEO_CODEC_VALUES else _DEFAULT_YOUTUBE_VIDEO_CODEC


def _codec_format_filters(video_codec: str) -> list[str]:
    normalized = _normalize_youtube_video_codec(video_codec)
    if normalized == "vp9":
        return ["vcodec^=vp9", "vcodec^=vp09"]
    if normalized == "av1":
        return ["vcodec^=av01", "vcodec^=av1"]
    return []


def _build_format_selector(max_height: int | None, video_codec: str = _DEFAULT_YOUTUBE_VIDEO_CODEC) -> str:
    if max_height and max_height > 0:
        if max_height <= 1080:
            return (
                f"best[height<={max_height}]/"
                f"bestvideo[height<={max_height}]+bestaudio/"
                "bestvideo+bestaudio/best"
            )
        codec_selectors = [
            f"bestvideo[height<={max_height}][{codec_filter}]+bestaudio"
            for codec_filter in _codec_format_filters(video_codec)
        ]
        codec_prefix = "/".join(codec_selectors)
        if codec_prefix:
            codec_prefix += "/"
        return (
            f"{codec_prefix}"
            f"bestvideo[height<={max_height}]+bestaudio/"
            f"best[height<={max_height}]/bestvideo+bestaudio/best"
        )
    return "bestvideo+bestaudio/best"


def _is_requested_format_unavailable_error(message: str) -> bool:
    lowered = str(message or "").lower()
    return "requested format is not available" in lowered


def _normalize_youtube_metadata_language(value: object) -> str:
    normalized = str(value or "").strip()
    return normalized if normalized in _YOUTUBE_METADATA_LANGUAGE_VALUES else ""


def _normalize_youtube_region(value: object) -> str:
    normalized = str(value or "").strip().upper()
    return normalized if normalized in _YOUTUBE_REGION_VALUES else ""


def _normalize_youtube_audio_preference(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _YOUTUBE_AUDIO_LANGUAGE_VALUES else ""


def _youtube_audio_lang_matches(preferred_lang: str, actual_lang: str) -> bool:
    preferred = _normalize_youtube_audio_preference(preferred_lang)
    actual = str(actual_lang or "").strip()
    if preferred == "zh":
        return actual == "zh" or actual.lower().startswith("zh-")
    if preferred == "en":
        return actual == "en"
    return False


def _normalize_youtube_subtitle_preference(value: object) -> str:
    normalized = str(value or "").strip()
    return normalized if normalized in _YOUTUBE_SUBTITLE_LANGUAGE_VALUES else ""


def _youtube_subtitle_lang_matches(preferred_lang: str, actual_lang: str) -> bool:
    preferred = _normalize_youtube_subtitle_preference(preferred_lang)
    if not preferred:
        return False
    return str(actual_lang or "").strip() in _YOUTUBE_SUBTITLE_LANGUAGE_ALIASES.get(preferred, {preferred})


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _looks_like_placeholder_title(title: str, source_url: str = "") -> bool:
    normalized = str(title or "").strip()
    if not normalized:
        return True
    if normalized in {"解析播放", "待解析"}:
        return True
    if normalized == str(source_url or "").strip():
        return True
    lowered = normalized.lower()
    return looks_like_youtube_video_id(normalized) or lowered.startswith(("http://", "https://", "yt:video:"))


def _resolve_startup_selection_height(
    qualities: list[VideoQualityOption],
    preferred_height: int | None,
) -> int | None:
    if not preferred_height or preferred_height <= 0:
        return None
    if any(option.height == preferred_height for option in qualities):
        return preferred_height
    return None


def _is_youtube_extractor(info: dict) -> bool:
    extractor = str(info.get("extractor") or "").strip().lower()
    return extractor.startswith("youtube")


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


def _append_detail_field(fields: list[PlaybackDetailField], label: str, value: object) -> None:
    normalized = str(value or "").strip()
    if not normalized:
        return
    fields.append(PlaybackDetailField(label=label, value=normalized))


def _build_detail_fields(info: dict) -> list[PlaybackDetailField]:
    fields: list[PlaybackDetailField] = []
    _append_detail_field(fields, "频道", info.get("channel") or info.get("uploader") or info.get("creator"))
    _append_detail_field(fields, "发布", _format_detail_date(info.get("upload_date") or info.get("release_date")))
    _append_detail_field(fields, "时长", _format_detail_duration(info.get("duration")))
    _append_detail_field(fields, "播放", _format_detail_stat_value(info.get("view_count")))
    _append_detail_field(fields, "点赞", _format_detail_stat_value(info.get("like_count")))
    _append_detail_field(fields, "评论", _format_detail_stat_value(info.get("comment_count")))
    _append_detail_field(fields, "简介", info.get("description"))
    return fields


def _merge_detail_fields(
    existing_fields: list[PlaybackDetailField],
    incoming_fields: list[PlaybackDetailField],
) -> list[PlaybackDetailField]:
    if not incoming_fields:
        return list(existing_fields)
    merged = list(existing_fields)
    label_to_index = {
        str(field.label).strip(): index
        for index, field in enumerate(merged)
        if str(field.label).strip()
    }
    for field in incoming_fields:
        label = str(field.label).strip()
        if not label:
            continue
        if label in label_to_index:
            merged[label_to_index[label]] = field
            continue
        label_to_index[label] = len(merged)
        merged.append(field)
    return merged


def _pick_requested_stream_pair(info: dict) -> tuple[str, str]:
    requested_formats = info.get("requested_formats") or []
    if not isinstance(requested_formats, list):
        return "", ""
    video_formats = [
        fmt for fmt in requested_formats
        if isinstance(fmt, dict) and fmt.get("url") and (fmt.get("vcodec", "") or "") != "none"
    ]
    audio_formats = [
        fmt for fmt in requested_formats
        if isinstance(fmt, dict) and fmt.get("url") and (fmt.get("acodec", "") or "") != "none"
    ]
    if not video_formats or not audio_formats:
        return "", ""
    return str(video_formats[0]["url"]), str(audio_formats[0]["url"])


def _video_codec_rank(vcodec: object) -> int:
    normalized = str(vcodec or "").lower()
    if normalized.startswith(("avc1", "h264")):
        return 0
    if normalized.startswith(("vp9", "vp09")):
        return 1
    if normalized.startswith(("av01", "av1")):
        return 2
    if normalized.startswith(("hev1", "hvc1", "hevc")):
        return 3
    return 4


def _video_codec_preference_rank(vcodec: object, preferred_codec: str) -> int:
    normalized = str(vcodec or "").lower()
    preference = _normalize_youtube_video_codec(preferred_codec)
    if preference == "vp9":
        if normalized.startswith(("vp9", "vp09")):
            return 0
        if normalized.startswith(("av01", "av1")):
            return 1
    elif preference == "av1":
        if normalized.startswith(("av01", "av1")):
            return 0
        if normalized.startswith(("vp9", "vp09")):
            return 1
    return _video_codec_rank(vcodec) + 2


def _audio_codec_rank(acodec: object, *, preferred_ext: str = "") -> int:
    normalized = str(acodec or "").lower()
    normalized_ext = str(preferred_ext or "").lower()
    if normalized_ext in {"mp4", "m4a"}:
        if normalized.startswith(("mp4a", "aac")):
            return 0
        if normalized.startswith("opus"):
            return 1
    if normalized.startswith("opus"):
        return 0
    if normalized.startswith(("mp4a", "aac")):
        return 1
    return 2


def _normalize_audio_lang(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    if not normalized:
        return ""
    if normalized in {"zh-hans", "zh-cn"}:
        return "zh-Hans"
    if normalized in {"zh-hant", "zh-tw"}:
        return "zh-Hant"
    if normalized.startswith(_ENGLISH_AUDIO_PREFIXES):
        return "en"
    if normalized.startswith(_CHINESE_AUDIO_PREFIXES):
        return "zh"
    return normalized


def _audio_candidate_is_original(fmt: dict) -> bool:
    haystacks = [
        str(fmt.get("format_note") or ""),
        str(fmt.get("format") or ""),
        str(fmt.get("name") or ""),
    ]
    tokens = " ".join(haystacks).casefold()
    return any(token in tokens for token in ("original", "orig", "source", "原声", "原版"))


def _audio_candidate_is_default(fmt: dict) -> bool:
    if bool(fmt.get("default")):
        return True
    try:
        return int(fmt.get("language_preference") or 0) > 0
    except (TypeError, ValueError):
        return False


def _audio_track_label(
    lang: str,
    *,
    is_original: bool,
    is_default: bool,
    fmt: dict,
) -> str:
    title = str(fmt.get("name") or fmt.get("format_note") or "").strip()
    if title and title.casefold() not in {"original", "dubbed"}:
        base = title
    else:
        base = {
            "en": "English",
            "zh": "中文",
        }.get(lang, _LANG_CODE_NAMES.get(lang, lang or "音轨"))
    suffixes: list[str] = []
    if is_original:
        suffixes.append("原声")
    if is_default and not is_original:
        suffixes.append("默认")
    if suffixes:
        return f"{base} ({'/'.join(suffixes)})"
    return base


def _audio_track_option_id(fmt: dict, lang: str) -> str:
    format_id = str(fmt.get("format_id") or "").strip() or "audio"
    normalized_lang = lang or "und"
    return f"ytdlp_audio_{normalized_lang}_{format_id}"


@dataclass(frozen=True, slots=True)
class _YtdlpAudioCandidate:
    option: YtdlpAudioTrackOption
    format_entry: dict
    is_muxed: bool = False


def _audio_track_sort_key(candidate: _YtdlpAudioCandidate) -> tuple[int, int, int, str, str]:
    option = candidate.option
    return (
        0 if option.lang == "en" and option.is_original else 1,
        0 if option.lang == "en" else 1,
        0 if option.is_default else 1,
        option.label.casefold(),
        option.format_id,
    )


def _audio_candidate_formats(info: dict) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    candidates: list[dict] = []
    for source_key in ("formats", "requested_formats"):
        source = info.get(source_key) or []
        if not isinstance(source, list):
            continue
        for fmt in source:
            if not isinstance(fmt, dict):
                continue
            if not fmt.get("url"):
                continue
            if (fmt.get("acodec", "") or "") == "none":
                continue
            if (fmt.get("vcodec", "") or "") != "none":
                continue
            key = (
                str(fmt.get("format_id") or "").strip(),
                str(fmt.get("url") or "").strip(),
            )
            if key in seen:
                continue
            seen.add(key)
            candidates.append(fmt)
    return candidates


def _best_muxed_audio_variant(variants: list[dict], max_height: int | None) -> dict | None:
    if not variants:
        return None
    if max_height and max_height > 0:
        bounded = [fmt for fmt in variants if int(fmt.get("height") or 0) <= max_height]
        if not bounded:
            return None
        variants = bounded
    return max(
        variants,
        key=lambda fmt: (
            int(fmt.get("height") or 0),
            int(fmt.get("tbr") or 0),
        ),
    )


def _build_muxed_audio_track_candidates(
    info: dict,
    max_height: int | None,
) -> list[_YtdlpAudioCandidate]:
    if not _is_youtube_extractor(info):
        return []
    grouped_variants: dict[str, list[dict]] = {}
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict) or not fmt.get("url"):
            continue
        if not _has_muxed_audio(fmt):
            continue
        if (fmt.get("vcodec", "") or "") == "none":
            continue
        lang = _normalize_audio_lang(fmt.get("language") or fmt.get("lang"))
        if not lang:
            continue
        grouped_variants.setdefault(lang, []).append(fmt)
    if len(grouped_variants) <= 1:
        return []

    candidates: list[_YtdlpAudioCandidate] = []
    for lang, variants in grouped_variants.items():
        selected_variant = _best_muxed_audio_variant(variants, max_height)
        if selected_variant is None:
            continue
        is_original = _audio_candidate_is_original(selected_variant)
        is_default = _audio_candidate_is_default(selected_variant)
        option = YtdlpAudioTrackOption(
            id=f"ytdlp_audio_{lang}_muxed",
            label=_audio_track_label(lang, is_original=is_original, is_default=is_default, fmt=selected_variant),
            lang=lang,
            format_id=str(selected_variant.get("format_id") or "").strip(),
            is_original=is_original,
            is_default=is_default,
            ytdl_format=str(selected_variant.get("format_id") or "").strip(),
        )
        candidates.append(_YtdlpAudioCandidate(option=option, format_entry=selected_variant, is_muxed=True))
    return sorted(candidates, key=_audio_track_sort_key)


def _build_audio_track_candidates(info: dict, max_height: int | None = None) -> list[_YtdlpAudioCandidate]:
    muxed_candidates = _build_muxed_audio_track_candidates(info, max_height)
    if muxed_candidates:
        return muxed_candidates
    candidates: list[_YtdlpAudioCandidate] = []
    for fmt in _audio_candidate_formats(info):
        lang = _normalize_audio_lang(fmt.get("language") or fmt.get("lang"))
        is_original = _audio_candidate_is_original(fmt)
        is_default = _audio_candidate_is_default(fmt)
        option = YtdlpAudioTrackOption(
            id=_audio_track_option_id(fmt, lang),
            label=_audio_track_label(lang, is_original=is_original, is_default=is_default, fmt=fmt),
            lang=lang,
            format_id=str(fmt.get("format_id") or "").strip(),
            is_original=is_original,
            is_default=is_default,
            ytdl_format=str(fmt.get("format_id") or "").strip(),
        )
        candidates.append(_YtdlpAudioCandidate(option=option, format_entry=fmt, is_muxed=False))
    return sorted(candidates, key=_audio_track_sort_key)


def _resolve_selected_audio_track_id(
    audio_tracks: list[YtdlpAudioTrackOption],
    requested_audio_track_id: str,
    *,
    fallback_format_id: str = "",
    default_audio_lang: str = "",
) -> str:
    requested = str(requested_audio_track_id or "").strip()
    if requested and any(track.id == requested for track in audio_tracks):
        return requested
    preferred_lang = _normalize_youtube_audio_preference(default_audio_lang)
    if preferred_lang:
        for track in audio_tracks:
            if _youtube_audio_lang_matches(preferred_lang, track.lang):
                return track.id
    for track in audio_tracks:
        if track.lang == "en" and track.is_original:
            return track.id
    for track in audio_tracks:
        if track.lang == "en":
            return track.id
    for track in audio_tracks:
        if track.is_default:
            return track.id
    fallback_format = str(fallback_format_id or "").strip()
    if fallback_format:
        for track in audio_tracks:
            if track.format_id == fallback_format:
                return track.id
    if audio_tracks:
        return audio_tracks[0].id
    return ""


def _select_audio_candidate(
    candidates: list[_YtdlpAudioCandidate],
    selected_audio_track_id: str,
) -> _YtdlpAudioCandidate | None:
    for candidate in candidates:
        if candidate.option.id == selected_audio_track_id:
            return candidate
    return None


def _audio_track_id_for_format(
    candidates: list[_YtdlpAudioCandidate],
    fmt: dict | None,
) -> str:
    format_id = str((fmt or {}).get("format_id") or "").strip()
    if not format_id:
        return ""
    for candidate in candidates:
        if candidate.option.format_id == format_id:
            return candidate.option.id
    return ""


def _preferred_video_formats(
    info: dict,
    max_height: int | None,
    *,
    video_codec: str = _DEFAULT_YOUTUBE_VIDEO_CODEC,
) -> list[dict]:
    formats = info.get("formats") or []
    candidates = [
        fmt for fmt in formats
        if isinstance(fmt, dict)
        and fmt.get("url")
        and (fmt.get("vcodec", "") or "") != "none"
        and int(fmt.get("height") or 0) > 0
        and (max_height is None or int(fmt.get("height") or 0) <= max_height)
    ]
    if not candidates:
        return []
    best_height = max(int(fmt.get("height") or 0) for fmt in candidates)
    filtered = [fmt for fmt in candidates if int(fmt.get("height") or 0) == best_height]
    if max_height and max_height > 1080:
        return sorted(
            filtered,
            key=lambda fmt: (
                _video_codec_preference_rank(fmt.get("vcodec"), video_codec),
                0 if _has_muxed_audio(fmt) else 1,
                -int(fmt.get("fps") or 0),
                -int(fmt.get("tbr") or 0),
            ),
        )
    return sorted(
        filtered,
        key=lambda fmt: (
            0 if _has_muxed_audio(fmt) else 1,
            _video_codec_rank(fmt.get("vcodec")),
            -int(fmt.get("fps") or 0),
            -int(fmt.get("tbr") or 0),
        ),
    )


def _preferred_audio_formats(info: dict, *, preferred_ext: str = "") -> list[dict]:
    formats = info.get("formats") or []
    candidates = [
        fmt for fmt in formats
        if isinstance(fmt, dict)
        and fmt.get("url")
        and (fmt.get("acodec", "") or "") != "none"
        and (fmt.get("vcodec", "") or "") == "none"
    ]
    return sorted(
        candidates,
        key=lambda fmt: (
            _audio_codec_rank(fmt.get("acodec"), preferred_ext=preferred_ext),
            -int(fmt.get("tbr") or 0),
        ),
    )


def _select_stream_pair(
    info: dict,
    max_height: int | None,
    *,
    video_codec: str = _DEFAULT_YOUTUBE_VIDEO_CODEC,
) -> tuple[dict | None, dict | None]:
    if max_height is None:
        requested_video_url, requested_audio_url = _pick_requested_stream_pair(info)
        if requested_video_url:
            requested_formats = info.get("requested_formats") or []
            selected_video = next(
                (
                    fmt for fmt in requested_formats
                    if isinstance(fmt, dict) and fmt.get("url") == requested_video_url
                ),
                None,
            )
            selected_audio = next(
                (
                    fmt for fmt in requested_formats
                    if isinstance(fmt, dict) and fmt.get("url") == requested_audio_url
                ),
                None,
            )
            return selected_video, selected_audio
    preferred_video_formats = _preferred_video_formats(info, max_height, video_codec=video_codec)
    if preferred_video_formats:
        selected_video = preferred_video_formats[0]
        if _has_muxed_audio(selected_video):
            return selected_video, None
        preferred_audio_formats = _preferred_audio_formats(info, preferred_ext=str(selected_video.get("ext") or ""))
        if preferred_audio_formats:
            return selected_video, preferred_audio_formats[0]
    return None, None


def _quality_id_or_default(max_height: int | None) -> str:
    if max_height and max_height > 0:
        return _quality_id_for_height(max_height)
    return "ytdlp_auto"


def _selected_ytdl_format(
    info: dict,
    selected_video: dict | None,
    selected_audio: dict | None,
    *,
    max_height: int | None,
    video_codec: str = _DEFAULT_YOUTUBE_VIDEO_CODEC,
) -> str:
    del info, selected_video, selected_audio
    return _build_format_selector(max_height, video_codec)


def _quality_option_ytdl_format(
    info: dict,
    video_format: dict,
    *,
    height: int,
    video_codec: str = _DEFAULT_YOUTUBE_VIDEO_CODEC,
) -> str:
    del info, video_format
    return _build_format_selector(height, video_codec)


def _pick_direct_url(
    info: dict,
    max_height: int | None,
    *,
    video_codec: str = _DEFAULT_YOUTUBE_VIDEO_CODEC,
) -> str:
    direct_url = info.get("url", "")
    requested_formats = info.get("requested_formats") or []
    if direct_url and not requested_formats:
        return direct_url
    selected_video, _selected_audio = _select_stream_pair(info, max_height, video_codec=video_codec)
    if selected_video is not None and selected_video.get("url"):
        return str(selected_video["url"])
    if direct_url:
        return direct_url
    formats = info.get("formats") or []
    muxed_formats = [fmt for fmt in formats if fmt.get("url") and _has_muxed_audio(fmt)]
    if muxed_formats:
        muxed_formats.sort(key=lambda fmt: (fmt.get("height") or 0, fmt.get("tbr") or 0), reverse=True)
        return str(muxed_formats[0]["url"])
    for fmt in formats:
        if fmt.get("url"):
            return str(fmt["url"])
    return ""


def _summarize_data_uri(url: str) -> str:
    if url.startswith(_DASH_DATA_URI_PREFIX):
        return f"{_DASH_DATA_URI_PREFIX}..."
    return url


def _format_mime_type(fmt: dict, *, content_type: str) -> str:
    mime_type = str(fmt.get("mime_type") or "").strip()
    if mime_type:
        return mime_type.partition(";")[0]
    ext = str(fmt.get("ext") or "").strip().lower()
    if content_type == "video":
        if ext == "webm":
            return "video/webm"
        return "video/mp4"
    if ext == "webm":
        return "audio/webm"
    return "audio/mp4"


def _format_codecs(fmt: dict, *, content_type: str) -> str:
    if content_type == "video":
        return str(fmt.get("vcodec") or "").strip()
    return str(fmt.get("acodec") or "").strip()


def _format_dash_byte_range(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    start = str(value.get("start") or "").strip()
    end = str(value.get("end") or "").strip()
    if not start.isdigit() or not end.isdigit():
        return ""
    return f"{start}-{end}"


def _dash_http_chunk_size(fmt: dict) -> int:
    downloader_options = fmt.get("downloader_options")
    if not isinstance(downloader_options, dict):
        return 0
    try:
        chunk_size = int(downloader_options.get("http_chunk_size") or 0)
    except (TypeError, ValueError):
        return 0
    return chunk_size if chunk_size > 0 else 0


def _dash_representation_xml(fmt: dict, *, content_type: str) -> str:
    attrs: list[tuple[str, str]] = [
        ("id", str(fmt.get("format_id") or "").strip() or content_type),
        ("mimeType", _format_mime_type(fmt, content_type=content_type)),
    ]
    codecs = _format_codecs(fmt, content_type=content_type)
    if codecs and codecs != "none":
        attrs.append(("codecs", codecs))
    tbr = int(float(fmt.get("tbr") or 0) * 1000)
    if tbr > 0:
        attrs.append(("bandwidth", str(tbr)))
    if content_type == "video":
        width = int(fmt.get("width") or 0)
        height = int(fmt.get("height") or 0)
        if width > 0:
            attrs.append(("width", str(width)))
        if height > 0:
            attrs.append(("height", str(height)))
    attributes = " ".join(f'{key}="{value}"' for key, value in attrs)
    index_range = _format_dash_byte_range(fmt.get("index_range"))
    init_range = _format_dash_byte_range(fmt.get("init_range"))
    segment_base_xml = ""
    if index_range or init_range:
        segment_base_attrs = f' indexRange="{index_range}"' if index_range else ""
        initialization_xml = f'<Initialization range="{init_range}"/>' if init_range else ""
        segment_base_xml = f"<SegmentBase{segment_base_attrs}>{initialization_xml}</SegmentBase>"
    chunk_size = _dash_http_chunk_size(fmt)
    chunk_size_xml = ""
    if chunk_size > 0:
        chunk_size_xml = (
            f'<SupplementalProperty schemeIdUri="{_DASH_HTTP_CHUNK_SIZE_SCHEME}" '
            f'value="{chunk_size}"/>'
        )
    return (
        f"<Representation {attributes}>"
        f"<BaseURL>{html.escape(str(fmt.get('url') or ''), quote=False)}</BaseURL>"
        f"{chunk_size_xml}"
        f"{segment_base_xml}"
        "</Representation>"
    )


def _has_dash_segment_metadata(fmt: dict | None) -> bool:
    if not isinstance(fmt, dict):
        return False
    return bool(_format_dash_byte_range(fmt.get("init_range")) or _format_dash_byte_range(fmt.get("index_range")))


def _merge_http_headers(*sources: object) -> dict[str, str]:
    merged: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        http_headers = source.get("http_headers") or {}
        if not isinstance(http_headers, dict):
            continue
        for key, value in http_headers.items():
            if isinstance(key, str) and isinstance(value, str):
                merged[key] = value
    return merged


def _build_dash_manifest_data_uri(
    video_format: dict,
    audio_format: dict,
    *,
    duration_seconds: int,
) -> str:
    duration_attr = ""
    if duration_seconds > 0:
        duration_attr = f' mediaPresentationDuration="PT{duration_seconds}S"'
    manifest = (
        f'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"{duration_attr}>'
        "<Period>"
        '<AdaptationSet contentType="video">'
        f"{_dash_representation_xml(video_format, content_type='video')}"
        "</AdaptationSet>"
        '<AdaptationSet contentType="audio">'
        f"{_dash_representation_xml(audio_format, content_type='audio')}"
        "</AdaptationSet>"
        "</Period>"
        "</MPD>"
    )
    encoded = base64.b64encode(manifest.encode("utf-8")).decode("ascii")
    return f"{_DASH_DATA_URI_PREFIX}{encoded}"


def _summarize_media_url(url: str) -> str:
    summarized_data_uri = _summarize_data_uri(url)
    if summarized_data_uri != url:
        return summarized_data_uri
    parsed = urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return url
    path = parsed.path or "/"
    if len(path) > 96:
        path = f"...{path[-96:]}"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


_YOUTUBE_VIDEO_ONLY_ITAGS = {
    "133",
    "134",
    "135",
    "136",
    "137",
    "138",
    "160",
    "212",
    "214",
    "216",
    "242",
    "243",
    "244",
    "247",
    "248",
    "264",
    "266",
    "271",
    "272",
    "278",
    "298",
    "299",
    "302",
    "303",
    "308",
    "313",
    "315",
    "330",
    "331",
    "332",
    "333",
    "334",
    "335",
    "336",
    "337",
    "394",
    "395",
    "396",
    "397",
    "398",
    "399",
    "400",
    "401",
    "402",
}


def _is_googlevideo_video_only_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    hostname = (parsed.hostname or "").lower()
    if hostname != "googlevideo.com" and not hostname.endswith(".googlevideo.com"):
        return False
    if not any(part == "videoplayback" for part in parsed.path.split("/") if part):
        return False
    query = parse_qs(parsed.query)
    itag = (query.get("itag") or [""])[0]
    return itag in _YOUTUBE_VIDEO_ONLY_ITAGS


@dataclass(frozen=True, slots=True)
class YtdlpResolveResult:
    url: str
    audio_url: str
    ytdl_format: str
    video_format_id: str
    audio_format_id: str
    audio_tracks: list[YtdlpAudioTrackOption]
    selected_audio_track_id: str
    title: str
    thumbnail: str
    description: str
    duration_seconds: int
    headers: dict[str, str]
    subtitles: list[ExternalSubtitleOption]
    qualities: list[VideoQualityOption]
    selected_quality_id: str
    extractor: str
    detail_fields: list[PlaybackDetailField]


@dataclass(slots=True)
class _YtdlpCacheEntry:
    result: YtdlpResolveResult
    expires_at: float


class YtdlpPlaybackService:
    def __init__(
        self,
        ttl_seconds: float = 300.0,
        now: Callable[[], float] = monotonic,
        proxy_decider: ProxyDecider | None = None,
        config_loader: Callable[[], AppConfig] | None = None,
    ) -> None:
        self._ytdlp_path: str | None = None
        self._supported_domains: frozenset[str] | None = None
        self._ttl_seconds = float(ttl_seconds)
        self._now = now
        self._cache: dict[str, _YtdlpCacheEntry] = {}
        self._proxy_decider = proxy_decider
        self._config_loader = config_loader

    def _youtube_cache_profile(self) -> tuple[str, str, str, str, str, str]:
        if self._config_loader is None:
            return ("", "", "", "", "", _DEFAULT_YOUTUBE_VIDEO_CODEC)
        config = self._config_loader()
        return (
            str(getattr(config, "youtube_cookie_browser", "") or "").strip().lower(),
            _normalize_youtube_audio_preference(getattr(config, "youtube_default_audio_lang", "")),
            _normalize_youtube_subtitle_preference(getattr(config, "youtube_default_subtitle_lang", "")),
            _normalize_youtube_metadata_language(getattr(config, "youtube_metadata_language", "")),
            _normalize_youtube_region(getattr(config, "youtube_region", "")),
            _normalize_youtube_video_codec(getattr(config, "youtube_video_codec", _DEFAULT_YOUTUBE_VIDEO_CODEC)),
        )

    def _cache_key(
        self,
        url: str,
        max_height: int | None,
        audio_track_id: str = "",
        *,
        include_subtitles: bool = False,
    ) -> str:
        key = _canonicalize_ytdlp_url(url)
        audio_key = str(audio_track_id or "").strip() or "auto"
        cookie_browser, default_audio, default_subtitle, metadata_language, region, video_codec = self._youtube_cache_profile()
        profile_key = (
            f"#cookie={cookie_browser or 'none'}"
            f"#da={default_audio or 'auto'}"
            f"#ds={default_subtitle or 'none'}"
            f"#hl={metadata_language or 'auto'}"
            f"#gl={region or 'auto'}"
            f"#vc={video_codec}"
            f"#subs={'all' if include_subtitles else 'none'}"
        )
        if max_height and max_height > 0:
            return f"{key}#h={max_height}#a={audio_key}{profile_key}"
        return f"{key}#h=any#a={audio_key}{profile_key}"

    def _get_cached_result(
        self,
        url: str,
        max_height: int | None,
        audio_track_id: str = "",
        *,
        include_subtitles: bool = False,
    ) -> YtdlpResolveResult | None:
        key = self._cache_key(url, max_height, audio_track_id, include_subtitles=include_subtitles)
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._now():
            self._cache.pop(key, None)
            return None
        return entry.result

    def _store_cached_result(
        self,
        url: str,
        max_height: int | None,
        audio_track_id: str,
        result: YtdlpResolveResult,
        *,
        include_subtitles: bool = False,
    ) -> None:
        entry = _YtdlpCacheEntry(
            result=result,
            expires_at=self._now() + self._ttl_seconds,
        )
        key = self._cache_key(url, max_height, audio_track_id, include_subtitles=include_subtitles)
        self._cache[key] = entry
        selected_height = _quality_height_from_id(str(result.selected_quality_id or ""))
        if selected_height is not None and selected_height != max_height:
            self._cache[
                self._cache_key(url, selected_height, audio_track_id, include_subtitles=include_subtitles)
            ] = entry

    def is_available(self) -> bool:
        if self._ytdlp_path is None:
            self._ytdlp_path = resolve_system_ytdlp_path()
        return bool(self._ytdlp_path)

    def _configured_cookie_browser(self) -> str:
        if self._config_loader is None:
            return ""
        config = self._config_loader()
        return str(getattr(config, "youtube_cookie_browser", "") or "").strip().lower()

    def _configured_default_audio_lang(self) -> str:
        if self._config_loader is None:
            return ""
        config = self._config_loader()
        return _normalize_youtube_audio_preference(getattr(config, "youtube_default_audio_lang", ""))

    def _configured_default_subtitle_lang(self) -> str:
        if self._config_loader is None:
            return ""
        config = self._config_loader()
        return _normalize_youtube_subtitle_preference(getattr(config, "youtube_default_subtitle_lang", ""))

    def _configured_metadata_language(self) -> str:
        if self._config_loader is None:
            return ""
        config = self._config_loader()
        return _normalize_youtube_metadata_language(getattr(config, "youtube_metadata_language", ""))

    def _configured_region(self) -> str:
        if self._config_loader is None:
            return ""
        config = self._config_loader()
        return _normalize_youtube_region(getattr(config, "youtube_region", ""))

    def _configured_video_codec(self) -> str:
        if self._config_loader is None:
            return _DEFAULT_YOUTUBE_VIDEO_CODEC
        config = self._config_loader()
        return _normalize_youtube_video_codec(
            getattr(config, "youtube_video_codec", _DEFAULT_YOUTUBE_VIDEO_CODEC)
        )

    def _resolved_title(
        self,
        result_title: object,
        *,
        vod: VodItem | None,
        item: PlayItem | None,
        source_url: str,
    ) -> str:
        title = str(result_title or "").strip()
        metadata_language = self._configured_metadata_language()
        if metadata_language.startswith("zh") and title and not _contains_cjk(title):
            existing_titles = [
                str(getattr(item, "title", "") or "").strip() if item is not None else "",
                str(getattr(item, "media_title", "") or "").strip() if item is not None else "",
                str(getattr(vod, "vod_name", "") or "").strip() if vod is not None else "",
            ]
            for existing_title in existing_titles:
                if (
                    _contains_cjk(existing_title)
                    and not _looks_like_placeholder_title(existing_title, source_url)
                ):
                    return existing_title
        return title or source_url

    def _configured_max_height(self) -> int | None:
        if self._config_loader is None:
            return None
        config = self._config_loader()
        try:
            max_height = int(getattr(config, "youtube_max_height", 0) or 0)
        except (TypeError, ValueError):
            return _DEFAULT_STARTUP_MAX_HEIGHT
        return max_height if max_height > 0 else _DEFAULT_STARTUP_MAX_HEIGHT

    def _extract_info_command(
        self,
        url: str,
        max_height: int | None,
        *,
        include_subtitles: bool,
        use_format_selector: bool = True,
    ) -> list[str]:
        if self._ytdlp_path is None:
            self._ytdlp_path = resolve_system_ytdlp_path()
        if not self._ytdlp_path:
            raise ValueError("yt-dlp 未安装")
        extra_args: list[str] = []
        metadata_language = self._configured_metadata_language()
        if metadata_language:
            extra_args.extend(["--extractor-args", f"youtube:lang={metadata_language}"])
        region = self._configured_region()
        if region:
            extra_args.extend(["--xff", region])
        format_args = [
            "--format",
            _build_format_selector(max_height, self._configured_video_codec()),
        ] if use_format_selector else []
        command = [
            self._ytdlp_path,
            "--no-warnings",
            "--dump-single-json",
            "--no-playlist",
            "--socket-timeout",
            "30",
            *format_args,
            *build_ytdlp_command_args(
                build_ytdlp_proxy_args(self._proxy_decider, url),
                cookie_browser=self._configured_cookie_browser(),
            ),
            *extra_args,
            "--",
            url,
        ]
        if include_subtitles:
            command[6:6] = [
                "--sub-format",
                "ass/srt/best",
                "--all-subs",
            ]
        return command

    def _extract_info_via_command(
        self,
        url: str,
        max_height: int | None,
        *,
        include_subtitles: bool = True,
        use_format_selector: bool = True,
    ) -> dict:
        command = self._extract_info_command(
            url,
            max_height,
            include_subtitles=include_subtitles,
            use_format_selector=use_format_selector,
        )
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("yt-dlp 解析超时") from exc
        stdout_text = (completed.stdout or "").strip()
        stderr_text = (completed.stderr or "").strip()
        if completed.returncode != 0:
            message = stderr_text or stdout_text or f"退出码 {completed.returncode}"
            if "geo" in message.lower():
                raise ValueError("该内容受地区限制")
            raise ValueError(f"下载错误: {message}")
        if not stdout_text:
            raise ValueError("yt-dlp 未返回结果")
        try:
            info = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"yt-dlp 解析失败: {exc}") from exc
        if not isinstance(info, dict):
            raise ValueError("yt-dlp 未返回结果")
        return info

    def _extract_fast_urls_command(
        self,
        url: str,
        max_height: int | None,
        *,
        include_browser_cookies: bool = False,
    ) -> list[str]:
        if self._ytdlp_path is None:
            self._ytdlp_path = resolve_system_ytdlp_path()
        if not self._ytdlp_path:
            raise ValueError("yt-dlp 未安装")
        extra_args: list[str] = []
        region = self._configured_region()
        if region:
            extra_args.extend(["--xff", region])
        command_args = build_ytdlp_command_args(
            build_ytdlp_proxy_args(self._proxy_decider, url),
            cookie_browser=self._configured_cookie_browser() if include_browser_cookies else "off",
        )
        return [
            self._ytdlp_path,
            "--no-warnings",
            "--no-playlist",
            "--socket-timeout",
            "30",
            "--format",
            _build_format_selector(max_height, self._configured_video_codec()),
            "--get-url",
            *command_args,
            *extra_args,
            "--",
            url,
        ]

    def _extract_fast_urls_via_command(
        self,
        url: str,
        max_height: int | None,
        *,
        include_browser_cookies: bool = False,
    ) -> list[str]:
        command = self._extract_fast_urls_command(
            url,
            max_height,
            include_browser_cookies=include_browser_cookies,
        )
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("yt-dlp 快速解析超时") from exc
        stdout_text = (completed.stdout or "").strip()
        stderr_text = (completed.stderr or "").strip()
        if completed.returncode != 0:
            raise ValueError(stderr_text or stdout_text or f"yt-dlp 退出码 {completed.returncode}")
        urls = [line.strip() for line in stdout_text.splitlines() if line.strip()]
        if not urls:
            raise ValueError("yt-dlp 未返回播放地址")
        return urls

    def _flat_playlist_command(self, url: str, *, page: int = 1, page_size: int = 30) -> list[str]:
        if self._ytdlp_path is None:
            self._ytdlp_path = resolve_system_ytdlp_path()
        if not self._ytdlp_path:
            raise ValueError("yt-dlp 未安装")
        page_number = max(1, int(page or 1))
        normalized_page_size = max(1, int(page_size or 30))
        start = (page_number - 1) * normalized_page_size + 1
        end = page_number * normalized_page_size
        extra_args: list[str] = []
        metadata_language = self._configured_metadata_language()
        if metadata_language:
            extra_args.extend(["--extractor-args", f"youtube:lang={metadata_language}"])
        region = self._configured_region()
        if region:
            extra_args.extend(["--xff", region])
        return [
            self._ytdlp_path,
            "--no-warnings",
            "--dump-single-json",
            "--flat-playlist",
            "--playlist-start",
            str(start),
            "--playlist-end",
            str(end),
            "--socket-timeout",
            "30",
            *build_ytdlp_command_args(
                build_ytdlp_proxy_args(self._proxy_decider, url),
                cookie_browser=self._configured_cookie_browser(),
            ),
            *extra_args,
            "--",
            url,
        ]

    def extract_flat_playlist(self, url: str, *, page: int = 1, page_size: int = 30) -> list[dict]:
        command = self._flat_playlist_command(url, page=page, page_size=page_size)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("yt-dlp 列表解析超时") from exc
        stdout_text = (completed.stdout or "").strip()
        stderr_text = (completed.stderr or "").strip()
        if completed.returncode != 0:
            raise ValueError(stderr_text or stdout_text or f"yt-dlp 退出码 {completed.returncode}")
        if not stdout_text:
            return []
        try:
            info = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"yt-dlp 列表解析失败: {exc}") from exc
        if not isinstance(info, dict):
            return []
        entries = info.get("entries")
        total = _playlist_total_from_info(info)
        if entries is None:
            return FlatPlaylistResult([info], total)
        if not isinstance(entries, list):
            return FlatPlaylistResult([], total)
        return FlatPlaylistResult([entry for entry in entries if isinstance(entry, dict)], total)

    def can_resolve(self, url: str) -> bool:
        if not self.is_available():
            return False
        candidate = _canonicalize_ytdlp_url(url)
        if not candidate:
            return False
        parsed = urlparse(candidate)
        hostname = (parsed.hostname or "").lower()
        if hostname in _KNOWN_YTDLP_DOMAINS:
            return True
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname in _KNOWN_YTDLP_DOMAINS

    def playback_format_selector(self, max_height: int | None = _DEFAULT_STARTUP_MAX_HEIGHT) -> str:
        return _build_format_selector(max_height, self._configured_video_codec())

    def playback_format_for_quality_id(self, quality_id: str) -> str:
        return _build_format_selector(_quality_height_from_id(quality_id), self._configured_video_codec())

    def resolve_fast(
        self,
        url: str,
        log: object = None,
        *,
        max_height: int | None = None,
    ) -> YtdlpResolveResult:
        canonical_url = _canonicalize_ytdlp_url(url)
        configured_default_height = self._configured_max_height() if max_height is None else None
        if max_height is None and configured_default_height is None:
            configured_default_height = _DEFAULT_STARTUP_MAX_HEIGHT
        fast_height = max_height if max_height is not None else configured_default_height
        logger.info("yt-dlp fast resolve start url=%s max_height=%s", canonical_url, fast_height)
        if callable(log):
            log("yt-dlp 正在快速提取播放地址...")
        started_at = monotonic()
        try:
            urls = self._extract_fast_urls_via_command(
                canonical_url,
                fast_height,
                include_browser_cookies=False,
            )
        except ValueError:
            if not self._configured_cookie_browser():
                raise
            urls = self._extract_fast_urls_via_command(
                canonical_url,
                fast_height,
                include_browser_cookies=True,
            )
        video_url = urls[0]
        audio_url = urls[1] if len(urls) > 1 else ""
        if not audio_url and _is_googlevideo_video_only_url(video_url):
            logger.warning(
                "yt-dlp fast resolve returned video-only stream without audio url=%s max_height=%s video=%s fallback=full_resolve",
                canonical_url,
                fast_height,
                _summarize_media_url(video_url),
            )
            return self.resolve(canonical_url, log=log, max_height=fast_height)
        result = YtdlpResolveResult(
            url=video_url,
            audio_url=audio_url,
            ytdl_format="",
            video_format_id="",
            audio_format_id="",
            audio_tracks=[],
            selected_audio_track_id="",
            title="",
            thumbnail="",
            description="",
            duration_seconds=0,
            headers={},
            subtitles=[],
            qualities=[],
            selected_quality_id=_quality_id_or_default(fast_height),
            extractor="",
            detail_fields=[],
        )
        logger.info(
            "yt-dlp fast resolve done url=%s max_height=%s elapsed=%.3fs video=%s audio=%s",
            canonical_url,
            fast_height,
            monotonic() - started_at,
            _summarize_media_url(result.url),
            _summarize_media_url(result.audio_url),
        )
        return result

    def resolve(
        self,
        url: str,
        log: object = None,
        *,
        max_height: int | None = None,
        selected_audio_track_id: str = "",
        include_subtitles: bool = False,
    ) -> YtdlpResolveResult:
        canonical_url = _canonicalize_ytdlp_url(url)
        configured_default_height = self._configured_max_height() if max_height is None else None
        cache_height = max_height if max_height is not None else configured_default_height
        extraction_max_height = max_height
        logger.info("yt-dlp resolve start url=%s max_height=%s", canonical_url, cache_height)
        cached = self._get_cached_result(
            canonical_url,
            cache_height,
            selected_audio_track_id,
            include_subtitles=include_subtitles,
        )
        if cached is not None:
            logger.info(
                "yt-dlp resolve cache-hit url=%s max_height=%s selected_quality=%s selected_audio=%s video=%s audio=%s",
                canonical_url,
                cache_height,
                cached.selected_quality_id,
                cached.selected_audio_track_id,
                _summarize_media_url(cached.url),
                _summarize_media_url(cached.audio_url),
            )
            if callable(log):
                log(f"yt-dlp 命中缓存 [{cached.extractor}]")
            return cached

        if callable(log):
            log("yt-dlp 正在提取视频信息...")
        started_at = monotonic()
        if not self.is_available():
            raise ValueError("yt-dlp 未安装")

        def extract_with_format_fallback(include_subtitles: bool) -> dict:
            try:
                return self._extract_info_via_command(
                    canonical_url,
                    extraction_max_height,
                    include_subtitles=include_subtitles,
                    use_format_selector=True,
                )
            except ValueError as exc:
                if not _is_requested_format_unavailable_error(str(exc)):
                    raise
                logger.warning(
                    "yt-dlp resolve requested format unavailable url=%s max_height=%s retry_without_format_selector=True",
                    canonical_url,
                    cache_height,
                )
                if callable(log):
                    log("yt-dlp 指定清晰度不可用，正在使用可用格式重试...")
                return self._extract_info_via_command(
                    canonical_url,
                    extraction_max_height,
                    include_subtitles=include_subtitles,
                    use_format_selector=False,
                )

        info = extract_with_format_fallback(include_subtitles=include_subtitles)

        if info is None:
            raise ValueError("yt-dlp 未返回结果")

        configured_video_codec = self._configured_video_codec()
        qualities = _build_quality_options(info, video_codec=configured_video_codec)
        selection_max_height = (
            max_height
            if max_height is not None
            else _resolve_startup_selection_height(qualities, configured_default_height)
        )

        selected_video, selected_audio = _select_stream_pair(
            info,
            selection_max_height,
            video_codec=configured_video_codec,
        )
        audio_candidates = _build_audio_track_candidates(info, selection_max_height)
        audio_tracks = [candidate.option for candidate in audio_candidates]
        fallback_audio_track_id = _audio_track_id_for_format(audio_candidates, selected_audio or selected_video)
        resolved_audio_track_id = _resolve_selected_audio_track_id(
            audio_tracks,
            selected_audio_track_id,
            fallback_format_id=str((selected_audio or {}).get("format_id") or ""),
            default_audio_lang=self._configured_default_audio_lang(),
        )
        preferred_audio_candidate = _select_audio_candidate(audio_candidates, resolved_audio_track_id)
        should_override_audio_selection = bool(str(selected_audio_track_id or "").strip()) or (
            resolved_audio_track_id != fallback_audio_track_id
        )
        if preferred_audio_candidate is not None and should_override_audio_selection:
            if preferred_audio_candidate.is_muxed:
                selected_video = preferred_audio_candidate.format_entry
                selected_audio = None
            elif selected_video is None or _has_muxed_audio(selected_video):
                video_only_candidates = [
                    fmt
                    for fmt in _preferred_video_formats(
                        info,
                        selection_max_height,
                        video_codec=configured_video_codec,
                    )
                    if not _has_muxed_audio(fmt)
                ]
                if video_only_candidates:
                    selected_video = video_only_candidates[0]
                    selected_audio = preferred_audio_candidate.format_entry
            else:
                selected_audio = preferred_audio_candidate.format_entry
        direct_url = _pick_direct_url(info, selection_max_height, video_codec=configured_video_codec)
        if selected_video is not None and selected_video.get("url") and _has_muxed_audio(selected_video):
            direct_url = str(selected_video["url"])
        if not direct_url:
            raise ValueError("未获取到播放地址")
        playback_url = direct_url
        requested_audio_url = ""
        ytdl_format = ""
        if selected_audio is not None and selected_audio.get("url"):
            if selected_video is None or not selected_video.get("url"):
                raise ValueError("未获取到视频流")
            preferred_youtube_video = None
            if _is_youtube_extractor(info):
                preferred_videos = _preferred_video_formats(
                    info,
                    selection_max_height,
                    video_codec=configured_video_codec,
                )
                candidate = preferred_videos[0] if preferred_videos else None
                if candidate is not None and _has_muxed_audio(candidate):
                    preferred_youtube_video = candidate
            if preferred_youtube_video is not None and preferred_youtube_video.get("url"):
                selected_video = preferred_youtube_video
            elif (
                _is_youtube_extractor(info)
                or _has_dash_segment_metadata(selected_video)
                or _has_dash_segment_metadata(selected_audio)
            ):
                playback_url = _build_dash_manifest_data_uri(
                    selected_video,
                    selected_audio,
                    duration_seconds=int(info.get("duration") or 0),
                )
            else:
                playback_url = str(selected_video["url"])
                requested_audio_url = str(selected_audio["url"])
            if preferred_youtube_video is not None and preferred_youtube_video.get("url"):
                playback_url = str(preferred_youtube_video["url"])

        headers = _merge_http_headers(info, selected_video, selected_audio)

        subtitles = _build_subtitle_options(info, preferred_lang=self._configured_default_subtitle_lang())
        detail_fields = _build_detail_fields(info)
        selected_quality_id = _resolve_selected_quality_id(
            info,
            qualities,
            selection_max_height,
            selected_video=selected_video,
        )

        if callable(log):
            ext = info.get("extractor", "")
            n_qual = len(qualities)
            n_sub = len(subtitles)
            log(f"yt-dlp 提取完成 [{ext}] 清晰度={n_qual} 字幕={n_sub}")

        result = YtdlpResolveResult(
            url=playback_url,
            audio_url=requested_audio_url,
            ytdl_format=ytdl_format,
            video_format_id=str((selected_video or {}).get("format_id") or ""),
            audio_format_id=str((selected_audio or {}).get("format_id") or ""),
            audio_tracks=audio_tracks,
            selected_audio_track_id=resolved_audio_track_id,
            title=info.get("title", ""),
            thumbnail=info.get("thumbnail", ""),
            description=info.get("description", ""),
            duration_seconds=int(info.get("duration") or 0),
            headers=headers,
            subtitles=subtitles,
            qualities=qualities,
            selected_quality_id=selected_quality_id,
            extractor=info.get("extractor", ""),
            detail_fields=detail_fields,
        )
        requested_formats = info.get("requested_formats") or []
        requested_format_ids = [
            str(fmt.get("format_id") or "")
            for fmt in requested_formats
            if isinstance(fmt, dict) and fmt.get("format_id")
        ]
        logger.info(
            "yt-dlp resolve done url=%s max_height=%s elapsed=%.3fs selected_quality=%s selected_audio=%s height=%s format_id=%s requested_formats=%s video=%s audio=%s",
            canonical_url,
            cache_height,
            monotonic() - started_at,
            selected_quality_id,
            resolved_audio_track_id,
            info.get("height") or 0,
            result.video_format_id or info.get("format_id") or "",
            requested_format_ids,
            _summarize_media_url(result.url),
            _summarize_media_url(result.audio_url),
        )
        self._store_cached_result(
            canonical_url,
            cache_height,
            selected_audio_track_id,
            result,
            include_subtitles=include_subtitles,
        )
        return result

    def resolve_for_quality(
        self,
        url: str,
        quality_id: str,
        log: object = None,
        *,
        audio_track_id: str = "",
        include_subtitles: bool = False,
    ) -> YtdlpResolveResult:
        max_height = _quality_height_from_id(quality_id)
        return self.resolve(
            url,
            log=log,
            max_height=max_height,
            selected_audio_track_id=audio_track_id,
            include_subtitles=include_subtitles,
        )

    def resolve_full(
        self,
        url: str,
        log: object = None,
        *,
        max_height: int | None = None,
        selected_audio_track_id: str = "",
    ) -> YtdlpResolveResult:
        return self.resolve(
            url,
            log=log,
            max_height=max_height,
            selected_audio_track_id=selected_audio_track_id,
            include_subtitles=True,
        )

    def resolve_for_quality_full(
        self,
        url: str,
        quality_id: str,
        log: object = None,
        *,
        audio_track_id: str = "",
    ) -> YtdlpResolveResult:
        return self.resolve_for_quality(
            url,
            quality_id,
            log=log,
            audio_track_id=audio_track_id,
            include_subtitles=True,
        )

    def apply_result(
        self,
        result: YtdlpResolveResult,
        *,
        vod: VodItem | None = None,
        item: PlayItem | None = None,
        source_url: str = "",
    ) -> None:
        resolved_source_url = (
            _canonicalize_ytdlp_url(str(source_url or ""))
            or (item.original_url if item is not None else "")
            or (item.vod_id if item is not None else "")
            or result.url
        )
        resolved_title = self._resolved_title(
            result.title,
            vod=vod,
            item=item,
            source_url=resolved_source_url,
        )

        if vod is not None:
            vod.vod_name = resolved_title
            vod.vod_pic = str(result.thumbnail or "")
            vod.vod_content = str(result.description or "")
            vod.detail_fields = _merge_detail_fields(vod.detail_fields, result.detail_fields)

        if item is None:
            return

        item.url = str(result.url or "")
        item.original_url = resolved_source_url
        item.headers = dict(result.headers)
        item.audio_url = str(result.audio_url or "")
        item.audio_tracks = list(result.audio_tracks)
        item.selected_audio_track_id = str(result.selected_audio_track_id or "").strip()
        item.ytdl_format = str(result.ytdl_format or "")
        item.playback_qualities = list(result.qualities)
        item.external_subtitles = list(result.subtitles)
        item.duration_seconds = int(result.duration_seconds or 0)
        item.title = resolved_title
        item.media_title = resolved_title
        item.detail_fields = _merge_detail_fields(item.detail_fields, result.detail_fields)
        if not item.selected_audio_track_id and item.audio_tracks:
            item.selected_audio_track_id = item.audio_tracks[0].id

        resolved_quality_id = str(result.selected_quality_id or "").strip()
        if resolved_quality_id:
            item.selected_playback_quality_id = resolved_quality_id
        elif item.playback_qualities:
            item.selected_playback_quality_id = item.playback_qualities[0].id
        else:
            item.selected_playback_quality_id = ""

    def resolve_to_play_item(
        self,
        url: str,
        *,
        max_height: int | None = None,
    ) -> tuple[VodItem, PlayItem]:
        resolved_url = _canonicalize_ytdlp_url(url)
        result = self.resolve(resolved_url, max_height=max_height)
        vod = VodItem(vod_id=resolved_url, vod_name=resolved_url)
        item = PlayItem(
            title=resolved_url,
            url="",
            original_url=resolved_url,
            vod_id=resolved_url,
            media_title=resolved_url,
        )
        self.apply_result(result, vod=vod, item=item, source_url=resolved_url)
        return vod, item


def _build_quality_options(
    info: dict,
    *,
    video_codec: str = _DEFAULT_YOUTUBE_VIDEO_CODEC,
) -> list[VideoQualityOption]:
    formats = info.get("formats") or []
    best_by_height: dict[int, dict] = {}
    is_youtube = _is_youtube_extractor(info)
    for fmt in formats:
        height = fmt.get("height")
        if not height or height < 360:
            continue
        vcodec = fmt.get("vcodec", "") or ""
        if vcodec == "none":
            continue
        previous = best_by_height.get(height)

        def sort_key(candidate: dict) -> tuple[int, int, int]:
            height_value = int(candidate.get("height") or 0)
            if is_youtube and height_value > 1080:
                return (
                    _video_codec_preference_rank(candidate.get("vcodec"), video_codec),
                    0 if _has_muxed_audio(candidate) else 1,
                    -int(candidate.get("tbr") or 0),
                )
            return (
                0 if is_youtube and _has_muxed_audio(candidate) else 1,
                _video_codec_rank(candidate.get("vcodec")),
                -int(candidate.get("tbr") or 0),
            )

        if previous is None or sort_key(fmt) < sort_key(previous):
            best_by_height[height] = fmt

    options: list[VideoQualityOption] = []
    for height in sorted(best_by_height.keys(), reverse=True):
        fmt = best_by_height[height]
        tbr = fmt.get("tbr")
        label = f"{height}p"
        if tbr:
            label += f"  {tbr:.0f}kbps"
        ytdl_format = ""
        option_url = ""
        if is_youtube:
            ytdl_format = _quality_option_ytdl_format(info, fmt, height=height, video_codec=video_codec)
            if fmt.get("url") and _has_muxed_audio(fmt):
                option_url = str(fmt.get("url") or "")
        options.append(VideoQualityOption(
            id=_quality_id_for_height(height),
            label=label,
            url=option_url,
            ytdl_format=ytdl_format,
            width=fmt.get("width") or 0,
            height=height,
            bandwidth=int((tbr or 0) * 1000),
            codecs=fmt.get("vcodec", "") or "",
        ))

    return options


def _resolve_selected_quality_id(
    info: dict,
    qualities: list[VideoQualityOption],
    max_height: int | None,
    *,
    selected_video: dict | None = None,
) -> str:
    selected_height = int((selected_video or {}).get("height") or 0)
    if not selected_height:
        selected_height = info.get("height") or 0
    if not selected_height:
        requested_formats = info.get("requested_formats") or []
        requested_heights = [
            int(fmt.get("height") or 0)
            for fmt in requested_formats
            if isinstance(fmt, dict) and fmt.get("height")
        ]
        if requested_heights:
            selected_height = max(requested_heights)
    if selected_height:
        selected_id = _quality_id_for_height(int(selected_height))
        if any(option.id == selected_id for option in qualities):
            return selected_id
    if max_height and max_height > 0:
        for option in qualities:
            if option.height <= max_height:
                return option.id
    if qualities:
        return qualities[0].id
    return ""


_ZH_EN_LANG_PREFIXES = ("en", "zh", "zh-", "zh_Hans", "zh_Hant", "zh-CN", "zh-TW")


def _is_chinese_or_english(lang_code: str) -> bool:
    return lang_code in ("en", "zh") or lang_code.startswith(_ZH_EN_LANG_PREFIXES)


def _build_subtitle_options(info: dict, *, preferred_lang: str = "") -> list[ExternalSubtitleOption]:
    result: list[ExternalSubtitleOption] = []
    seen_urls: set[str] = set()

    for source_key in ("subtitles", "automatic_captions"):
        subs_dict = info.get(source_key) or {}
        is_auto = source_key == "automatic_captions"
        for lang_code, subs in subs_dict.items():
            if not _is_chinese_or_english(lang_code):
                continue
            if not isinstance(subs, list):
                continue
            supported_candidates: list[dict] = []
            for sub in subs:
                if not isinstance(sub, dict):
                    continue
                sub_url = str(sub.get("url", "") or "").strip()
                if not sub_url or sub_url in seen_urls:
                    continue
                ext = _normalized_direct_subtitle_format(sub)
                if ext is None:
                    continue
                supported_candidates.append({"url": sub_url, "ext": ext})

            if not supported_candidates:
                continue

            supported_candidates.sort(key=lambda candidate: _DIRECT_SUBTITLE_FORMAT_RANK[candidate["ext"]])
            best_candidate = supported_candidates[0]
            seen_urls.add(best_candidate["url"])
            lang_name = _LANG_CODE_NAMES.get(lang_code, lang_code)
            name = lang_name
            if is_auto:
                name += " (自动生成)"
            name += " [yt-dlp]"
            result.append(ExternalSubtitleOption(
                name=name,
                lang=lang_code,
                url=best_candidate["url"],
                format=best_candidate["ext"],
                source="ytdlp",
            ))

    normalized_preference = _normalize_youtube_subtitle_preference(preferred_lang)
    if normalized_preference:
        result.sort(
            key=lambda subtitle: 0
            if _youtube_subtitle_lang_matches(normalized_preference, subtitle.lang)
            else 1
        )
    return result


def _normalized_direct_subtitle_format(sub: dict) -> str | None:
    sub_url = str(sub.get("url", "") or "").strip()
    if _is_translated_youtube_caption_url(sub_url):
        return None

    ext = str(sub.get("ext", "") or "").strip().lower()
    if ext in _DIRECT_SUBTITLE_FORMAT_RANK:
        return "srt" if ext == "subrip" else ext

    parsed_path = urlparse(sub_url.lower()).path
    if "." in parsed_path:
        suffix = parsed_path.rsplit(".", 1)[-1]
        if suffix in _DIRECT_SUBTITLE_FORMAT_RANK:
            return "srt" if suffix == "subrip" else suffix
    return None


def _is_translated_youtube_caption_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc not in {"www.youtube.com", "youtube.com", "m.youtube.com"}:
        return False
    if parsed.path != "/api/timedtext":
        return False
    return bool(parse_qs(parsed.query).get("tlang"))
