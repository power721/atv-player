import json
import hashlib
import platform
from pathlib import Path
import sys
import time
from typing import Any, cast
import uuid

from atv_player.models import AppConfig, AppIdentity
from atv_player.source_preferences import (
    VALID_DANMAKU_PROVIDER_IDS,
    VALID_METADATA_PROVIDER_IDS,
    VALID_SUBTITLE_PROVIDER_IDS,
)
from atv_player.sqlite_utils import managed_connection

_VALID_DANMAKU_RENDER_MODES = {"static", "scroll_only", "mixed"}
_VALID_DANMAKU_COLOR_MODES = {"uniform", "source"}
_VALID_DANMAKU_POSITION_PRESETS = {"top", "upper", "mid_upper", "bottom"}
_VALID_DANMAKU_OUTLINE_STRENGTHS = {"off", "soft", "strong"}
_VALID_THEME_MODES = {"light", "dark", "system"}
_VALID_NETWORK_PROXY_MODES = {"direct", "system", "http", "https", "socks5"}
_VALID_YOUTUBE_COOKIE_BROWSERS = {"", "chrome", "edge", "firefox"}
_VALID_YOUTUBE_VIDEO_CODECS = {"vp9", "av1", "auto"}
_VALID_YOUTUBE_SUBTITLE_LANGS = {"", "zh-CN", "zh-TW", "zh-HK", "en"}
_VALID_YOUTUBE_AUDIO_LANGS = {"", "zh", "en"}
_VALID_YOUTUBE_REGIONS = {"", "CN", "US", "JP", "SG", "HK", "TW"}
_VALID_YOUTUBE_CATEGORY_SOURCE_TYPES = {"builtin", "remote", "local"}
_VALID_MPV_RENDER_PROFILES = {
    "auto",
    "compat",
    "balanced",
    "vulkan",
    "quality",
    "performance",
    "copy-back",
    "software",
}
_VALID_MPV_HWDEC_MODES = {"auto-safe", "auto-copy", "no"}
_VALID_FOLLOWING_EPISODE_DISPLAY_MODES = {"compact", "poster", "full"}
_VALID_FOLLOWING_EPISODE_GRID_COLUMNS = {1, 2, 3}
_VALID_HOME_MODES = {"browse", "classic", "simplified", "media", "tv"}
_GLOBAL_SEARCH_HISTORY_LIMIT = 50
_APP_IDENTITY_HASH_LENGTH = 16
_DEFAULT_NETWORK_PROXY_BYPASS_RULES = [
    "localhost",
    "127.0.0.1",
    "::1",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    ".local",
]


def _normalize_danmaku_line_count(value: object) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(normalized, 10))


def _normalize_danmaku_blocked_words(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        word = raw.strip()
        if not word or word in seen:
            continue
        seen.add(word)
        output.append(word)
    return output


def _normalize_danmaku_duplicate_window_minutes(value: object) -> int:
    try:
        return max(0, min(int(cast(Any, value)), 60))
    except (TypeError, ValueError):
        return 0


def _normalize_danmaku_render_mode(value: object) -> str:
    text = str(value or "").strip()
    return text if text in _VALID_DANMAKU_RENDER_MODES else "static"


def _normalize_danmaku_color_mode(value: object) -> str:
    text = str(value or "").strip()
    return text if text in _VALID_DANMAKU_COLOR_MODES else "source"


def _normalize_danmaku_uniform_color(value: object) -> str:
    text = str(value or "").strip().upper()
    if len(text) == 7 and text.startswith("#"):
        return text
    return "#FFFFFF"


def _normalize_danmaku_position_preset(value: object) -> str:
    text = str(value or "").strip()
    return text if text in _VALID_DANMAKU_POSITION_PRESETS else "top"


def _normalize_danmaku_scroll_speed(value: object) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.5, min(round(normalized, 2), 2.0))


def _normalize_danmaku_font_size(value: object) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 32
    return max(16, min(normalized, 72))


def _normalize_danmaku_opacity(value: object) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 85
    clamped = max(30, min(normalized, 100))
    return max(30, min(int(round(clamped / 5) * 5), 100))


def _normalize_danmaku_outline_strength(value: object) -> str:
    text = str(value or "").strip()
    return text if text in _VALID_DANMAKU_OUTLINE_STRENGTHS else "strong"


def _normalize_global_search_history(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    history: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        history.append(text)
        seen.add(text)
    return history[:_GLOBAL_SEARCH_HISTORY_LIMIT]


def _normalize_ai_base_url(value: object) -> str:
    return str(value or "").strip()


def _normalize_ai_secret(value: object) -> str:
    return str(value or "").strip()


def _normalize_ai_model(value: object) -> str:
    return str(value or "").strip()


def _normalize_ai_timeout(value: object) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return 30
    return max(5, min(timeout, 120))


def _normalize_disabled_provider_ids(value: object, valid_ids: set[str]) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen or text not in valid_ids:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def _normalize_theme_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_THEME_MODES else "system"


def _normalize_logging_enabled(value: object) -> bool:
    return bool(value)


def _normalize_network_proxy_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_NETWORK_PROXY_MODES else "direct"


def _normalize_network_proxy_url(value: object) -> str:
    return str(value or "").strip()


def _normalize_tmdb_proxy_base_url(value: object) -> str:
    return str(value or "").strip().rstrip("/")


def _normalize_network_proxy_bypass_rules(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return list(_DEFAULT_NETWORK_PROXY_BYPASS_RULES)
    if value is None:
        return list(_DEFAULT_NETWORK_PROXY_BYPASS_RULES)
    if not isinstance(value, list):
        return list(_DEFAULT_NETWORK_PROXY_BYPASS_RULES)
    rules: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        rules.append(text)
        seen.add(text)
    return rules


def _normalize_network_proxy_rules(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    rules: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        rules.append(text)
        seen.add(text)
    return rules


def _normalize_youtube_cookie_browser(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_YOUTUBE_COOKIE_BROWSERS else ""


def _normalize_youtube_max_height(value: object) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 1080
    return normalized if normalized in {480, 720, 1080, 1440, 2160} else 1080


def _normalize_youtube_video_codec(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_YOUTUBE_VIDEO_CODECS else "vp9"


def _normalize_youtube_subtitle_lang(value: object) -> str:
    text = str(value or "").strip()
    return text if text in _VALID_YOUTUBE_SUBTITLE_LANGS else ""


def _normalize_youtube_audio_lang(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_YOUTUBE_AUDIO_LANGS else ""


def _normalize_youtube_metadata_language(value: object) -> str:
    text = str(value or "").strip()
    return text if text in _VALID_YOUTUBE_SUBTITLE_LANGS else ""


def _normalize_youtube_region(value: object) -> str:
    text = str(value or "").strip().upper()
    return text if text in _VALID_YOUTUBE_REGIONS else ""


def _normalize_youtube_category_source_type(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_YOUTUBE_CATEGORY_SOURCE_TYPES else "builtin"


def _normalize_youtube_category_source_value(value: object) -> str:
    return str(value or "").strip()


def _normalize_youtube_category_cache_refreshed_at(value: object) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, normalized)


def _normalize_mpv_cache_size_mb(value: object) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 512
    return max(16, min(normalized, 4096))


def _render_profile_from_legacy_hwdec(value: object) -> str:
    hwdec = _normalize_mpv_hwdec_mode(value)
    if hwdec == "no":
        return "software"
    if hwdec == "auto-copy":
        return "balanced"
    return "auto"


def _normalize_mpv_render_profile(value: object, legacy_hwdec: object = "auto-safe") -> str:
    text = str(value or "").strip().lower()
    if text in _VALID_MPV_RENDER_PROFILES:
        return text
    return _render_profile_from_legacy_hwdec(legacy_hwdec)


def _normalize_mpv_hwdec_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_MPV_HWDEC_MODES else "auto-safe"


def _normalize_mpv_network_timeout_seconds(value: object) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 15
    return max(1, min(normalized, 300))


def _normalize_mpv_default_readahead_secs(value: object) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 20
    return max(1, min(normalized, 600))


def _normalize_mpv_extra_options(value: object) -> str:
    return str(value or "").strip()


def _normalize_playback_auto_switch_source_on_failure(value: object) -> bool:
    return bool(value)


def _normalize_bilibili_grouped_playlist_tree_enabled(value: object) -> bool:
    return bool(value)


def _normalize_m3u_proxy_segment_prefetch_size(value: object) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 2
    return max(0, min(normalized, 10))


_VALID_M3U8_AD_FILTER_MODES = {"off", "markers", "smart"}


def _normalize_m3u8_ad_filter_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_M3U8_AD_FILTER_MODES else "smart"


def _normalize_following_episode_display_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_FOLLOWING_EPISODE_DISPLAY_MODES else "poster"


def _normalize_following_episode_grid_columns(value: object) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 1
    return normalized if normalized in _VALID_FOLLOWING_EPISODE_GRID_COLUMNS else 1


def _following_episode_grid_columns_from_legacy_mode(value: object) -> int:
    normalized = _normalize_following_episode_display_mode(value)
    if normalized == "compact":
        return 2
    return 1


def _normalize_home_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_HOME_MODES else "browse"


class SettingsRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        return managed_connection(self._db_path)

    @property
    def database_path(self) -> Path:
        return self._db_path

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_identity (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    installation_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS app_identity_no_update
                BEFORE UPDATE ON app_identity
                BEGIN
                    SELECT RAISE(ABORT, 'app identity is immutable');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS app_identity_no_delete
                BEFORE DELETE ON app_identity
                BEGIN
                    SELECT RAISE(ABORT, 'app identity is immutable');
                END
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    base_url TEXT NOT NULL,
                    username TEXT NOT NULL,
                    token TEXT NOT NULL,
                    vod_token TEXT NOT NULL,
                    theme_mode TEXT NOT NULL DEFAULT 'system',
                    logging_enabled INTEGER NOT NULL DEFAULT 1,
                    metadata_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                    episode_title_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                    disabled_danmaku_provider_ids TEXT NOT NULL DEFAULT '[]',
                    danmaku_blocked_words TEXT NOT NULL DEFAULT '[]',
                    danmaku_duplicate_window_minutes INTEGER NOT NULL DEFAULT 0,
                    danmaku_convert_top_bottom_to_scroll INTEGER NOT NULL DEFAULT 0,
                    dandan_base_url TEXT NOT NULL DEFAULT '',
                    bangumi_data_danmaku_enabled INTEGER NOT NULL DEFAULT 0,
                    subtitle_subdl_api_key TEXT NOT NULL DEFAULT '',
                    subtitle_assrt_token TEXT NOT NULL DEFAULT '',
                    subtitle_opensubtitles_api_key TEXT NOT NULL DEFAULT '',
                    subtitle_subsource_api_key TEXT NOT NULL DEFAULT '',
                    disabled_subtitle_provider_ids TEXT NOT NULL DEFAULT '[]',
                    disabled_metadata_provider_ids TEXT NOT NULL DEFAULT '[]',
                    metadata_douban_cookie TEXT NOT NULL DEFAULT '',
                    metadata_tmdb_api_key TEXT NOT NULL DEFAULT '',
                    metadata_tmdb_proxy_base_url TEXT NOT NULL DEFAULT '',
                    metadata_bangumi_access_token TEXT NOT NULL DEFAULT '',
                    network_proxy_mode TEXT NOT NULL DEFAULT 'direct',
                    network_proxy_url TEXT NOT NULL DEFAULT '',
                    network_proxy_bypass_rules TEXT NOT NULL DEFAULT '["localhost","127.0.0.1","::1","10.0.0.0/8","172.16.0.0/12","192.168.0.0/16",".local"]',
                    youtube_cookie_browser TEXT NOT NULL DEFAULT '',
                    youtube_max_height INTEGER NOT NULL DEFAULT 1080,
                    youtube_video_codec TEXT NOT NULL DEFAULT 'vp9',
                    youtube_default_subtitle_lang TEXT NOT NULL DEFAULT '',
                    youtube_default_audio_lang TEXT NOT NULL DEFAULT '',
                    youtube_metadata_language TEXT NOT NULL DEFAULT '',
                    youtube_region TEXT NOT NULL DEFAULT '',
                    youtube_category_source_type TEXT NOT NULL DEFAULT 'builtin',
                    youtube_category_source_value TEXT NOT NULL DEFAULT '',
                    youtube_category_cache_json TEXT NOT NULL DEFAULT '',
                    youtube_category_cache_refreshed_at INTEGER NOT NULL DEFAULT 0,
                    youtube_category_cache_error TEXT NOT NULL DEFAULT '',
                    mpv_render_profile TEXT NOT NULL DEFAULT 'auto',
                    mpv_cache_size_mb INTEGER NOT NULL DEFAULT 512,
                    mpv_hwdec_mode TEXT NOT NULL DEFAULT 'auto-safe',
                    mpv_network_timeout_seconds INTEGER NOT NULL DEFAULT 15,
                    mpv_default_readahead_secs INTEGER NOT NULL DEFAULT 20,
                    mpv_extra_options TEXT NOT NULL DEFAULT '',
                    playback_auto_switch_source_on_failure INTEGER NOT NULL DEFAULT 0,
                    bilibili_grouped_playlist_tree_enabled INTEGER NOT NULL DEFAULT 0,
                    m3u_proxy_segment_prefetch_size INTEGER NOT NULL DEFAULT 2,
                    m3u8_ad_filter_mode TEXT NOT NULL DEFAULT 'smart',
                    last_path TEXT NOT NULL,
                    last_active_window TEXT NOT NULL DEFAULT 'main',
                    last_playback_source TEXT NOT NULL DEFAULT 'browse',
                    last_playback_source_key TEXT NOT NULL DEFAULT '',
                    last_playback_mode TEXT NOT NULL DEFAULT '',
                    last_playback_path TEXT NOT NULL DEFAULT '',
                    last_playback_vod_id TEXT NOT NULL DEFAULT '',
                    last_playback_clicked_vod_id TEXT NOT NULL DEFAULT '',
                    last_player_paused INTEGER NOT NULL DEFAULT 0,
                    player_volume INTEGER NOT NULL DEFAULT 100,
                    player_muted INTEGER NOT NULL DEFAULT 0,
                    player_wide_mode INTEGER NOT NULL DEFAULT 0,
                    player_log_visible INTEGER NOT NULL DEFAULT 1,
                    preferred_parse_key TEXT NOT NULL DEFAULT '',
                    preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1,
                    preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1,
                    preferred_danmaku_render_mode TEXT NOT NULL DEFAULT 'static',
                    preferred_danmaku_color_mode TEXT NOT NULL DEFAULT 'source',
                    preferred_danmaku_uniform_color TEXT NOT NULL DEFAULT '#FFFFFF',
                    preferred_danmaku_position_preset TEXT NOT NULL DEFAULT 'top',
                    preferred_danmaku_scroll_speed REAL NOT NULL DEFAULT 1.0,
                    preferred_danmaku_font_size INTEGER NOT NULL DEFAULT 32,
                    preferred_danmaku_opacity INTEGER NOT NULL DEFAULT 85,
                    preferred_danmaku_outline_strength TEXT NOT NULL DEFAULT 'strong',
                    main_window_geometry BLOB,
                    player_window_geometry BLOB,
                    player_main_splitter_state BLOB,
                    browse_content_splitter_state BLOB,
                    last_selected_tab TEXT NOT NULL DEFAULT 'douban',
                    last_selected_category_tab TEXT NOT NULL DEFAULT '',
                    last_selected_category_id TEXT NOT NULL DEFAULT '',
                    builtin_tab_overrides_json TEXT NOT NULL DEFAULT '',
                    global_search_history TEXT NOT NULL DEFAULT '[]',
                    global_search_hot_source TEXT NOT NULL DEFAULT '360',
                    ai_enabled INTEGER NOT NULL DEFAULT 0,
                    ai_base_url TEXT NOT NULL DEFAULT '',
                    ai_api_key TEXT NOT NULL DEFAULT '',
                    ai_chat_model TEXT NOT NULL DEFAULT '',
                    ai_request_timeout_seconds INTEGER NOT NULL DEFAULT 30,
                    ai_metadata_enrichment_enabled INTEGER NOT NULL DEFAULT 1,
                    ai_danmaku_enrichment_enabled INTEGER NOT NULL DEFAULT 1,
                    ai_episode_title_rewrite_enabled INTEGER NOT NULL DEFAULT 1,
                    ai_following_summary_enabled INTEGER NOT NULL DEFAULT 1,
                    following_episode_display_mode TEXT NOT NULL DEFAULT 'poster',
                    following_episode_grid_columns INTEGER NOT NULL DEFAULT 1,
                    following_backend_enabled INTEGER NOT NULL DEFAULT 0,
                    following_backend_auto_subscribe INTEGER NOT NULL DEFAULT 0,
                    home_mode TEXT NOT NULL DEFAULT 'browse'
                )
                """
            )
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(app_config)").fetchall()
            }
            if "vod_token" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN vod_token TEXT NOT NULL DEFAULT ''"
                )
            if "theme_mode" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN theme_mode TEXT NOT NULL DEFAULT 'system'"
                )
            if "logging_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN logging_enabled INTEGER NOT NULL DEFAULT 1"
                )
            if "metadata_enhancement_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN metadata_enhancement_enabled INTEGER NOT NULL DEFAULT 1"
                )
            if "episode_title_enhancement_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN episode_title_enhancement_enabled INTEGER NOT NULL DEFAULT 1"
                )
            if "disabled_danmaku_provider_ids" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN disabled_danmaku_provider_ids TEXT NOT NULL DEFAULT '[]'"
                )
            if "danmaku_blocked_words" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN danmaku_blocked_words TEXT NOT NULL DEFAULT '[]'"
                )
            if "danmaku_duplicate_window_minutes" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN danmaku_duplicate_window_minutes INTEGER NOT NULL DEFAULT 0"
                )
            if "danmaku_convert_top_bottom_to_scroll" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN danmaku_convert_top_bottom_to_scroll INTEGER NOT NULL DEFAULT 0"
                )
            if "dandan_base_url" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN dandan_base_url TEXT NOT NULL DEFAULT ''"
                )
            if "bangumi_data_danmaku_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN bangumi_data_danmaku_enabled INTEGER NOT NULL DEFAULT 0"
                )
            if "subtitle_subdl_api_key" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN subtitle_subdl_api_key TEXT NOT NULL DEFAULT ''"
                )
            if "subtitle_assrt_token" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN subtitle_assrt_token TEXT NOT NULL DEFAULT ''"
                )
            if "subtitle_opensubtitles_api_key" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN subtitle_opensubtitles_api_key TEXT NOT NULL DEFAULT ''"
                )
            if "subtitle_subsource_api_key" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN subtitle_subsource_api_key TEXT NOT NULL DEFAULT ''"
                )
            if "disabled_subtitle_provider_ids" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN disabled_subtitle_provider_ids TEXT NOT NULL DEFAULT '[]'"
                )
            if "disabled_metadata_provider_ids" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN disabled_metadata_provider_ids TEXT NOT NULL DEFAULT '[]'"
                )
            if "metadata_douban_cookie" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN metadata_douban_cookie TEXT NOT NULL DEFAULT ''"
                )
            if "metadata_tmdb_api_key" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN metadata_tmdb_api_key TEXT NOT NULL DEFAULT ''"
                )
            if "metadata_tmdb_proxy_base_url" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN metadata_tmdb_proxy_base_url TEXT NOT NULL DEFAULT ''"
                )
            if "metadata_bangumi_access_token" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN metadata_bangumi_access_token TEXT NOT NULL DEFAULT ''"
                )
            if "network_proxy_mode" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN network_proxy_mode TEXT NOT NULL DEFAULT 'direct'"
                )
            if "network_proxy_url" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN network_proxy_url TEXT NOT NULL DEFAULT ''"
                )
            if "network_proxy_bypass_rules" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN network_proxy_bypass_rules TEXT NOT NULL DEFAULT '[\"localhost\",\"127.0.0.1\",\"::1\",\"10.0.0.0/8\",\"172.16.0.0/12\",\"192.168.0.0/16\",\".local\"]'"
                )
            if "youtube_cookie_browser" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN youtube_cookie_browser TEXT NOT NULL DEFAULT ''"
                )
            if "youtube_max_height" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN youtube_max_height INTEGER NOT NULL DEFAULT 1080"
                )
            if "youtube_video_codec" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN youtube_video_codec TEXT NOT NULL DEFAULT 'vp9'"
                )
            if "youtube_default_subtitle_lang" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN youtube_default_subtitle_lang TEXT NOT NULL DEFAULT ''"
                )
            if "youtube_default_audio_lang" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN youtube_default_audio_lang TEXT NOT NULL DEFAULT ''"
                )
            if "youtube_metadata_language" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN youtube_metadata_language TEXT NOT NULL DEFAULT ''"
                )
            if "youtube_region" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN youtube_region TEXT NOT NULL DEFAULT ''"
                )
            if "youtube_category_source_type" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN youtube_category_source_type TEXT NOT NULL DEFAULT 'builtin'"
                )
            if "youtube_category_source_value" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN youtube_category_source_value TEXT NOT NULL DEFAULT ''"
                )
            if "youtube_category_cache_json" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN youtube_category_cache_json TEXT NOT NULL DEFAULT ''"
                )
            if "youtube_category_cache_refreshed_at" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN youtube_category_cache_refreshed_at INTEGER NOT NULL DEFAULT 0"
                )
            if "youtube_category_cache_error" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN youtube_category_cache_error TEXT NOT NULL DEFAULT ''"
                )
            if "mpv_cache_size_mb" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN mpv_cache_size_mb INTEGER NOT NULL DEFAULT 512"
                )
            if "mpv_render_profile" not in columns:
                legacy_hwdec = "auto-safe"
                if "mpv_hwdec_mode" in columns:
                    row = conn.execute(
                        "SELECT mpv_hwdec_mode FROM app_config WHERE id = 1"
                    ).fetchone()
                    if row is not None:
                        legacy_hwdec = row[0]
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN mpv_render_profile TEXT NOT NULL DEFAULT 'auto'"
                )
                conn.execute(
                    "UPDATE app_config SET mpv_render_profile = ? WHERE id = 1",
                    (_render_profile_from_legacy_hwdec(legacy_hwdec),),
                )
            if "mpv_hwdec_mode" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN mpv_hwdec_mode TEXT NOT NULL DEFAULT 'auto-safe'"
                )
            if "mpv_network_timeout_seconds" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN mpv_network_timeout_seconds INTEGER NOT NULL DEFAULT 15"
                )
            if "mpv_default_readahead_secs" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN mpv_default_readahead_secs INTEGER NOT NULL DEFAULT 20"
                )
            if "mpv_extra_options" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN mpv_extra_options TEXT NOT NULL DEFAULT ''"
                )
            if "playback_auto_switch_source_on_failure" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN playback_auto_switch_source_on_failure INTEGER NOT NULL DEFAULT 0"
                )
            if "bilibili_grouped_playlist_tree_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN bilibili_grouped_playlist_tree_enabled INTEGER NOT NULL DEFAULT 0"
                )
            if "m3u_proxy_segment_prefetch_size" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN m3u_proxy_segment_prefetch_size INTEGER NOT NULL DEFAULT 2"
                )
            if "m3u8_ad_filter_mode" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN m3u8_ad_filter_mode TEXT NOT NULL DEFAULT 'smart'"
                )
            if "last_active_window" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_active_window TEXT NOT NULL DEFAULT 'main'"
                )
            if "last_playback_source" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_playback_source TEXT NOT NULL DEFAULT 'browse'"
                )
            if "last_playback_source_key" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_playback_source_key TEXT NOT NULL DEFAULT ''"
                )
            if "last_playback_mode" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_playback_mode TEXT NOT NULL DEFAULT ''"
                )
            if "last_playback_path" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_playback_path TEXT NOT NULL DEFAULT ''"
                )
            if "last_playback_vod_id" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_playback_vod_id TEXT NOT NULL DEFAULT ''"
                )
            if "last_playback_clicked_vod_id" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_playback_clicked_vod_id TEXT NOT NULL DEFAULT ''"
                )
            if "last_player_paused" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_player_paused INTEGER NOT NULL DEFAULT 0"
                )
            if "player_volume" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN player_volume INTEGER NOT NULL DEFAULT 100"
                )
            if "player_muted" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN player_muted INTEGER NOT NULL DEFAULT 0"
                )
            if "player_wide_mode" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN player_wide_mode INTEGER NOT NULL DEFAULT 0"
                )
            if "player_log_visible" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN player_log_visible INTEGER NOT NULL DEFAULT 1"
                )
            if "preferred_parse_key" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN preferred_parse_key TEXT NOT NULL DEFAULT ''"
                )
            if "preferred_danmaku_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1"
                )
            if "preferred_danmaku_line_count" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1"
                )
            if "preferred_danmaku_render_mode" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN preferred_danmaku_render_mode TEXT NOT NULL DEFAULT 'static'"
                )
            if "preferred_danmaku_color_mode" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN preferred_danmaku_color_mode TEXT NOT NULL DEFAULT 'source'"
                )
            if "preferred_danmaku_uniform_color" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN preferred_danmaku_uniform_color TEXT NOT NULL DEFAULT '#FFFFFF'"
                )
            if "preferred_danmaku_position_preset" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN preferred_danmaku_position_preset TEXT NOT NULL DEFAULT 'top'"
                )
            if "preferred_danmaku_scroll_speed" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN preferred_danmaku_scroll_speed REAL NOT NULL DEFAULT 1.0"
                )
            if "preferred_danmaku_font_size" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN preferred_danmaku_font_size INTEGER NOT NULL DEFAULT 32"
                )
            if "preferred_danmaku_opacity" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN preferred_danmaku_opacity INTEGER NOT NULL DEFAULT 85"
                )
            if "preferred_danmaku_outline_strength" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN preferred_danmaku_outline_strength TEXT NOT NULL DEFAULT 'strong'"
                )
            if "main_window_geometry" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN main_window_geometry BLOB"
                )
            if "player_window_geometry" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN player_window_geometry BLOB"
                )
            if "player_main_splitter_state" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN player_main_splitter_state BLOB"
                )
            if "browse_content_splitter_state" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN browse_content_splitter_state BLOB"
                )
            if "last_selected_tab" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_selected_tab TEXT NOT NULL DEFAULT 'douban'"
                )
            if "last_selected_category_tab" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_selected_category_tab TEXT NOT NULL DEFAULT ''"
                )
            if "last_selected_category_id" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_selected_category_id TEXT NOT NULL DEFAULT ''"
                )
            if "builtin_tab_overrides_json" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN builtin_tab_overrides_json TEXT NOT NULL DEFAULT ''"
                )
            if "global_search_history" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN global_search_history TEXT NOT NULL DEFAULT '[]'"
                )
            if "global_search_hot_source" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN global_search_hot_source TEXT NOT NULL DEFAULT '360'"
                )
            if "ai_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN ai_enabled INTEGER NOT NULL DEFAULT 0"
                )
            if "ai_base_url" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN ai_base_url TEXT NOT NULL DEFAULT ''"
                )
            if "ai_api_key" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN ai_api_key TEXT NOT NULL DEFAULT ''"
                )
            if "ai_chat_model" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN ai_chat_model TEXT NOT NULL DEFAULT ''"
                )
            if "ai_request_timeout_seconds" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN ai_request_timeout_seconds INTEGER NOT NULL DEFAULT 30"
                )
            if "ai_metadata_enrichment_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN ai_metadata_enrichment_enabled INTEGER NOT NULL DEFAULT 1"
                )
            if "ai_danmaku_enrichment_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN ai_danmaku_enrichment_enabled INTEGER NOT NULL DEFAULT 1"
                )
            if "ai_episode_title_rewrite_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN ai_episode_title_rewrite_enabled INTEGER NOT NULL DEFAULT 1"
                )
            if "ai_following_summary_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN ai_following_summary_enabled INTEGER NOT NULL DEFAULT 1"
                )
            if "following_episode_display_mode" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN following_episode_display_mode TEXT NOT NULL DEFAULT 'poster'"
                )
            if "following_episode_grid_columns" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN following_episode_grid_columns INTEGER NOT NULL DEFAULT 1"
                )
            if "following_backend_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN following_backend_enabled INTEGER NOT NULL DEFAULT 0"
                )
            if "following_backend_auto_subscribe" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN following_backend_auto_subscribe INTEGER NOT NULL DEFAULT 0"
                )
            if "network_proxy_rules" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN network_proxy_rules TEXT NOT NULL DEFAULT '[]'"
                )
            if "home_mode" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN home_mode TEXT NOT NULL DEFAULT 'browse'"
                )
            conn.execute(
                """
                INSERT INTO app_config (
                    id,
                    base_url,
                    username,
                    token,
                    vod_token,
                    theme_mode,
                    logging_enabled,
                    metadata_enhancement_enabled,
                    episode_title_enhancement_enabled,
                    disabled_danmaku_provider_ids,
                    danmaku_blocked_words,
                    danmaku_duplicate_window_minutes,
                    danmaku_convert_top_bottom_to_scroll,
                    disabled_metadata_provider_ids,
                    metadata_douban_cookie,
                    metadata_tmdb_api_key,
                    metadata_tmdb_proxy_base_url,
                    metadata_bangumi_access_token,
                    network_proxy_mode,
                    network_proxy_url,
                    network_proxy_bypass_rules,
                    youtube_cookie_browser,
                    youtube_max_height,
                    youtube_video_codec,
                    youtube_default_subtitle_lang,
                    youtube_default_audio_lang,
                    youtube_metadata_language,
                    youtube_region,
                    youtube_category_source_type,
                    youtube_category_source_value,
                    youtube_category_cache_json,
                    youtube_category_cache_refreshed_at,
                    youtube_category_cache_error,
                    mpv_render_profile,
                    mpv_cache_size_mb,
                    mpv_hwdec_mode,
                    mpv_network_timeout_seconds,
                    mpv_default_readahead_secs,
                    mpv_extra_options,
                    playback_auto_switch_source_on_failure,
                    bilibili_grouped_playlist_tree_enabled,
                    m3u_proxy_segment_prefetch_size,
                    m3u8_ad_filter_mode,
                    last_path,
                    last_active_window,
                    last_playback_source,
                    last_playback_source_key,
                    last_playback_mode,
                    last_playback_path,
                    last_playback_vod_id,
                    last_playback_clicked_vod_id,
                    last_player_paused,
                    player_volume,
                    player_muted,
                    player_wide_mode,
                    player_log_visible,
                    preferred_parse_key,
                    preferred_danmaku_enabled,
                    preferred_danmaku_line_count,
                    preferred_danmaku_render_mode,
                    preferred_danmaku_color_mode,
                    preferred_danmaku_uniform_color,
                    preferred_danmaku_position_preset,
                    preferred_danmaku_scroll_speed,
                    preferred_danmaku_font_size,
                    preferred_danmaku_opacity,
                    preferred_danmaku_outline_strength,
                    main_window_geometry,
                    player_window_geometry,
                    player_main_splitter_state,
                    browse_content_splitter_state,
                    last_selected_tab,
                    last_selected_category_tab,
                    last_selected_category_id,
                    builtin_tab_overrides_json,
                    global_search_history,
                    global_search_hot_source,
                    ai_enabled,
                    ai_base_url,
                    ai_api_key,
                    ai_chat_model,
                    ai_request_timeout_seconds,
                    ai_metadata_enrichment_enabled,
                    ai_danmaku_enrichment_enabled,
                    ai_episode_title_rewrite_enabled,
                    ai_following_summary_enabled,
                    following_episode_display_mode,
                    following_episode_grid_columns,
                    following_backend_enabled,
                    following_backend_auto_subscribe,
                    home_mode
                )
                VALUES (
                    1, 'http://127.0.0.1:4567', '', '', '', 'system', 1, 1, 1, '[]', '[]', 0, 0, '[]', '', '', '', '', 'direct', '', '["localhost","127.0.0.1","::1","10.0.0.0/8","172.16.0.0/12","192.168.0.0/16",".local"]', '', 1080, 'vp9', '', '', '', '', 'builtin', '', '', 0, '', 'auto', 512, 'auto-safe', 15, 20, '', 0, 0, 2, 'smart', '/', 'main', 'browse', '', '', '', '', '',
                    0, 100, 0, 0, 1, '', 1, 1, 'static', 'source', '#FFFFFF', 'top', 1.0, 32, 85, 'strong',
                    NULL, NULL, NULL, NULL, 'douban', '', '', '', '[]', '360', 0, '', '', '', 30, 1, 1, 1, 1, 'poster', 1, 0, 0, 'browse'
                )
                ON CONFLICT(id) DO NOTHING
                """
            )

    def _build_app_identity_id(self) -> str:
        installation_uuid = str(uuid.uuid4())
        feature_text = "|".join(
            (
                "atv-player",
                installation_uuid,
                sys.platform,
                platform.machine(),
                platform.release(),
                str(self._db_path.resolve()),
            )
        )
        feature_hash = hashlib.sha256(feature_text.encode("utf-8")).hexdigest()[
            :_APP_IDENTITY_HASH_LENGTH
        ]
        return f"{installation_uuid}.{feature_hash}"

    def ensure_app_identity(self) -> AppIdentity:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT installation_id, created_at
                FROM app_identity
                WHERE id = 1
                """
            ).fetchone()
            if row is None:
                installation_id = self._build_app_identity_id()
                created_at = int(time.time())
                conn.execute(
                    """
                    INSERT INTO app_identity (id, installation_id, created_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (installation_id, created_at),
                )
                row = conn.execute(
                    """
                    SELECT installation_id, created_at
                    FROM app_identity
                    WHERE id = 1
                    """
                ).fetchone()
        assert row is not None
        return AppIdentity(installation_id=str(row[0]), created_at=int(row[1]))

    def load_config(self) -> AppConfig:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    base_url,
                    username,
                    token,
                    vod_token,
                    theme_mode,
                    logging_enabled,
                    metadata_enhancement_enabled,
                    episode_title_enhancement_enabled,
                    disabled_danmaku_provider_ids,
                    danmaku_blocked_words,
                    danmaku_duplicate_window_minutes,
                    danmaku_convert_top_bottom_to_scroll,
                    disabled_metadata_provider_ids,
                    metadata_douban_cookie,
                    metadata_tmdb_api_key,
                    metadata_tmdb_proxy_base_url,
                    metadata_bangumi_access_token,
                    network_proxy_mode,
                    network_proxy_url,
                    network_proxy_bypass_rules,
                    network_proxy_rules,
                    youtube_cookie_browser,
                    youtube_max_height,
                    youtube_video_codec,
                    youtube_default_subtitle_lang,
                    youtube_default_audio_lang,
                    youtube_metadata_language,
                    youtube_region,
                    youtube_category_source_type,
                    youtube_category_source_value,
                    youtube_category_cache_json,
                    youtube_category_cache_refreshed_at,
                    youtube_category_cache_error,
                    mpv_render_profile,
                    mpv_cache_size_mb,
                    mpv_hwdec_mode,
                    mpv_network_timeout_seconds,
                    mpv_default_readahead_secs,
                    mpv_extra_options,
                    playback_auto_switch_source_on_failure,
                    bilibili_grouped_playlist_tree_enabled,
                    m3u_proxy_segment_prefetch_size,
                    m3u8_ad_filter_mode,
                    last_path,
                    last_active_window,
                    last_playback_source,
                    last_playback_source_key,
                    last_playback_mode,
                    last_playback_path,
                    last_playback_vod_id,
                    last_playback_clicked_vod_id,
                    last_player_paused,
                    player_volume,
                    player_muted,
                    player_wide_mode,
                    player_log_visible,
                    preferred_parse_key,
                    preferred_danmaku_enabled,
                    preferred_danmaku_line_count,
                    preferred_danmaku_render_mode,
                    preferred_danmaku_color_mode,
                    preferred_danmaku_uniform_color,
                    preferred_danmaku_position_preset,
                    preferred_danmaku_scroll_speed,
                    preferred_danmaku_font_size,
                    preferred_danmaku_opacity,
                    preferred_danmaku_outline_strength,
                    main_window_geometry,
                    player_window_geometry,
                    player_main_splitter_state,
                    browse_content_splitter_state,
                    last_selected_tab,
                    last_selected_category_tab,
                    last_selected_category_id,
                    builtin_tab_overrides_json,
                    global_search_history,
                    global_search_hot_source,
                    ai_enabled,
                    ai_base_url,
                    ai_api_key,
                    ai_chat_model,
                    ai_request_timeout_seconds,
                    ai_metadata_enrichment_enabled,
                    ai_danmaku_enrichment_enabled,
                    ai_episode_title_rewrite_enabled,
                    ai_following_summary_enabled,
                    following_episode_display_mode,
                    following_episode_grid_columns,
                    following_backend_enabled,
                    following_backend_auto_subscribe,
                    home_mode,
                    dandan_base_url,
                    bangumi_data_danmaku_enabled,
                    subtitle_subdl_api_key,
                    subtitle_assrt_token,
                    subtitle_opensubtitles_api_key,
                    subtitle_subsource_api_key,
                    disabled_subtitle_provider_ids
                FROM app_config
                WHERE id = 1
                """
            ).fetchone()
        assert row is not None
        (
            base_url,
            username,
            token,
            vod_token,
            theme_mode,
            logging_enabled,
            metadata_enhancement_enabled,
            episode_title_enhancement_enabled,
            disabled_danmaku_provider_ids,
            danmaku_blocked_words,
            danmaku_duplicate_window_minutes,
            danmaku_convert_top_bottom_to_scroll,
            disabled_metadata_provider_ids,
            metadata_douban_cookie,
            metadata_tmdb_api_key,
            metadata_tmdb_proxy_base_url,
            metadata_bangumi_access_token,
            network_proxy_mode,
            network_proxy_url,
            network_proxy_bypass_rules,
            network_proxy_rules,
            youtube_cookie_browser,
            youtube_max_height,
            youtube_video_codec,
            youtube_default_subtitle_lang,
            youtube_default_audio_lang,
            youtube_metadata_language,
            youtube_region,
            youtube_category_source_type,
            youtube_category_source_value,
            youtube_category_cache_json,
            youtube_category_cache_refreshed_at,
            youtube_category_cache_error,
            mpv_render_profile,
            mpv_cache_size_mb,
            mpv_hwdec_mode,
            mpv_network_timeout_seconds,
            mpv_default_readahead_secs,
            mpv_extra_options,
            playback_auto_switch_source_on_failure,
            bilibili_grouped_playlist_tree_enabled,
            m3u_proxy_segment_prefetch_size,
            m3u8_ad_filter_mode,
            last_path,
            last_active_window,
            last_playback_source,
            last_playback_source_key,
            last_playback_mode,
            last_playback_path,
            last_playback_vod_id,
            last_playback_clicked_vod_id,
            last_player_paused,
            player_volume,
            player_muted,
            player_wide_mode,
            player_log_visible,
            preferred_parse_key,
            preferred_danmaku_enabled,
            preferred_danmaku_line_count,
            preferred_danmaku_render_mode,
            preferred_danmaku_color_mode,
            preferred_danmaku_uniform_color,
            preferred_danmaku_position_preset,
            preferred_danmaku_scroll_speed,
            preferred_danmaku_font_size,
            preferred_danmaku_opacity,
            preferred_danmaku_outline_strength,
            main_window_geometry,
            player_window_geometry,
            player_main_splitter_state,
            browse_content_splitter_state,
            last_selected_tab,
            last_selected_category_tab,
            last_selected_category_id,
            builtin_tab_overrides_json,
            global_search_history,
            global_search_hot_source,
            ai_enabled,
            ai_base_url,
            ai_api_key,
            ai_chat_model,
            ai_request_timeout_seconds,
            ai_metadata_enrichment_enabled,
            ai_danmaku_enrichment_enabled,
            ai_episode_title_rewrite_enabled,
            ai_following_summary_enabled,
            following_episode_display_mode,
            following_episode_grid_columns,
            following_backend_enabled,
            following_backend_auto_subscribe,
            home_mode,
            dandan_base_url,
            bangumi_data_danmaku_enabled,
            subtitle_subdl_api_key,
            subtitle_assrt_token,
            subtitle_opensubtitles_api_key,
            subtitle_subsource_api_key,
            disabled_subtitle_provider_ids,
        ) = row
        return AppConfig(
            base_url=base_url,
            username=username,
            token=token,
            vod_token=vod_token,
            theme_mode=_normalize_theme_mode(theme_mode),
            logging_enabled=_normalize_logging_enabled(logging_enabled),
            metadata_enhancement_enabled=bool(metadata_enhancement_enabled),
            episode_title_enhancement_enabled=bool(episode_title_enhancement_enabled),
            disabled_danmaku_provider_ids=_normalize_disabled_provider_ids(
                disabled_danmaku_provider_ids,
                VALID_DANMAKU_PROVIDER_IDS,
            ),
            danmaku_blocked_words=_normalize_danmaku_blocked_words(
                danmaku_blocked_words
            ),
            danmaku_duplicate_window_minutes=_normalize_danmaku_duplicate_window_minutes(
                danmaku_duplicate_window_minutes
            ),
            danmaku_convert_top_bottom_to_scroll=bool(
                danmaku_convert_top_bottom_to_scroll
            ),
            disabled_metadata_provider_ids=_normalize_disabled_provider_ids(
                disabled_metadata_provider_ids,
                VALID_METADATA_PROVIDER_IDS,
            ),
            metadata_douban_cookie=str(metadata_douban_cookie or "").strip(),
            metadata_tmdb_api_key=str(metadata_tmdb_api_key or "").strip(),
            metadata_tmdb_proxy_base_url=_normalize_tmdb_proxy_base_url(metadata_tmdb_proxy_base_url),
            metadata_bangumi_access_token=str(metadata_bangumi_access_token or "").strip(),
            network_proxy_mode=_normalize_network_proxy_mode(network_proxy_mode),
            network_proxy_url=_normalize_network_proxy_url(network_proxy_url),
            network_proxy_bypass_rules=_normalize_network_proxy_bypass_rules(network_proxy_bypass_rules),
            network_proxy_rules=_normalize_network_proxy_rules(network_proxy_rules),
            youtube_cookie_browser=_normalize_youtube_cookie_browser(youtube_cookie_browser),
            youtube_max_height=_normalize_youtube_max_height(youtube_max_height),
            youtube_video_codec=_normalize_youtube_video_codec(youtube_video_codec),
            youtube_default_subtitle_lang=_normalize_youtube_subtitle_lang(youtube_default_subtitle_lang),
            youtube_default_audio_lang=_normalize_youtube_audio_lang(youtube_default_audio_lang),
            youtube_metadata_language=_normalize_youtube_metadata_language(youtube_metadata_language),
            youtube_region=_normalize_youtube_region(youtube_region),
            youtube_category_source_type=_normalize_youtube_category_source_type(youtube_category_source_type),
            youtube_category_source_value=_normalize_youtube_category_source_value(youtube_category_source_value),
            youtube_category_cache_json=str(youtube_category_cache_json or ""),
            youtube_category_cache_refreshed_at=_normalize_youtube_category_cache_refreshed_at(
                youtube_category_cache_refreshed_at
            ),
            youtube_category_cache_error=str(youtube_category_cache_error or "").strip(),
            mpv_render_profile=_normalize_mpv_render_profile(
                mpv_render_profile,
                mpv_hwdec_mode,
            ),
            mpv_cache_size_mb=_normalize_mpv_cache_size_mb(mpv_cache_size_mb),
            mpv_hwdec_mode=_normalize_mpv_hwdec_mode(mpv_hwdec_mode),
            mpv_network_timeout_seconds=_normalize_mpv_network_timeout_seconds(mpv_network_timeout_seconds),
            mpv_default_readahead_secs=_normalize_mpv_default_readahead_secs(mpv_default_readahead_secs),
            mpv_extra_options=_normalize_mpv_extra_options(mpv_extra_options),
            playback_auto_switch_source_on_failure=_normalize_playback_auto_switch_source_on_failure(
                playback_auto_switch_source_on_failure
            ),
            bilibili_grouped_playlist_tree_enabled=_normalize_bilibili_grouped_playlist_tree_enabled(
                bilibili_grouped_playlist_tree_enabled
            ),
            m3u_proxy_segment_prefetch_size=_normalize_m3u_proxy_segment_prefetch_size(
                m3u_proxy_segment_prefetch_size
            ),
            m3u8_ad_filter_mode=_normalize_m3u8_ad_filter_mode(m3u8_ad_filter_mode),
            last_path=last_path,
            last_active_window=last_active_window,
            last_playback_source=last_playback_source,
            last_playback_source_key=last_playback_source_key,
            last_playback_mode=last_playback_mode,
            last_playback_path=last_playback_path,
            last_playback_vod_id=last_playback_vod_id,
            last_playback_clicked_vod_id=last_playback_clicked_vod_id,
            last_player_paused=bool(last_player_paused),
            player_volume=player_volume,
            player_muted=bool(player_muted),
            player_wide_mode=bool(player_wide_mode),
            player_log_visible=bool(player_log_visible),
            preferred_parse_key=preferred_parse_key,
            preferred_danmaku_enabled=bool(preferred_danmaku_enabled),
            preferred_danmaku_line_count=_normalize_danmaku_line_count(preferred_danmaku_line_count),
            preferred_danmaku_render_mode=_normalize_danmaku_render_mode(preferred_danmaku_render_mode),
            preferred_danmaku_color_mode=_normalize_danmaku_color_mode(preferred_danmaku_color_mode),
            preferred_danmaku_uniform_color=_normalize_danmaku_uniform_color(preferred_danmaku_uniform_color),
            preferred_danmaku_position_preset=_normalize_danmaku_position_preset(preferred_danmaku_position_preset),
            preferred_danmaku_scroll_speed=_normalize_danmaku_scroll_speed(preferred_danmaku_scroll_speed),
            preferred_danmaku_font_size=_normalize_danmaku_font_size(preferred_danmaku_font_size),
            preferred_danmaku_opacity=_normalize_danmaku_opacity(preferred_danmaku_opacity),
            preferred_danmaku_outline_strength=_normalize_danmaku_outline_strength(preferred_danmaku_outline_strength),
            main_window_geometry=main_window_geometry,
            player_window_geometry=player_window_geometry,
            player_main_splitter_state=player_main_splitter_state,
            browse_content_splitter_state=browse_content_splitter_state,
            last_selected_tab=last_selected_tab,
            last_selected_category_tab=last_selected_category_tab,
            last_selected_category_id=last_selected_category_id,
            builtin_tab_overrides_json=str(builtin_tab_overrides_json or "").strip(),
            global_search_history=_normalize_global_search_history(global_search_history),
            global_search_hot_source=str(global_search_hot_source or "360").strip() or "360",
            ai_enabled=bool(ai_enabled),
            ai_base_url=_normalize_ai_base_url(ai_base_url),
            ai_api_key=_normalize_ai_secret(ai_api_key),
            ai_chat_model=_normalize_ai_model(ai_chat_model),
            ai_request_timeout_seconds=_normalize_ai_timeout(ai_request_timeout_seconds),
            ai_metadata_enrichment_enabled=bool(ai_metadata_enrichment_enabled),
            ai_danmaku_enrichment_enabled=bool(ai_danmaku_enrichment_enabled),
            ai_episode_title_rewrite_enabled=bool(ai_episode_title_rewrite_enabled),
            ai_following_summary_enabled=bool(ai_following_summary_enabled),
            following_episode_display_mode=_normalize_following_episode_display_mode(
                following_episode_display_mode
            ),
            following_episode_grid_columns=_normalize_following_episode_grid_columns(
                following_episode_grid_columns
                if following_episode_grid_columns is not None
                else _following_episode_grid_columns_from_legacy_mode(
                    following_episode_display_mode
                )
            ),
            following_backend_enabled=bool(following_backend_enabled),
            following_backend_auto_subscribe=bool(following_backend_auto_subscribe),
            home_mode=_normalize_home_mode(home_mode),
            dandan_base_url=str(dandan_base_url or "").strip(),
            bangumi_data_danmaku_enabled=bool(bangumi_data_danmaku_enabled),
            subtitle_subdl_api_key=str(subtitle_subdl_api_key or "").strip(),
            subtitle_assrt_token=str(subtitle_assrt_token or "").strip(),
            subtitle_opensubtitles_api_key=str(
                subtitle_opensubtitles_api_key or ""
            ).strip(),
            subtitle_subsource_api_key=str(
                subtitle_subsource_api_key or ""
            ).strip(),
            disabled_subtitle_provider_ids=_normalize_disabled_provider_ids(
                disabled_subtitle_provider_ids,
                VALID_SUBTITLE_PROVIDER_IDS,
            ),
        )

    def save_config(self, config: AppConfig) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE app_config
                SET
                    base_url = ?,
                    username = ?,
                    token = ?,
                    vod_token = ?,
                    theme_mode = ?,
                    logging_enabled = ?,
                    metadata_enhancement_enabled = ?,
                    episode_title_enhancement_enabled = ?,
                    disabled_danmaku_provider_ids = ?,
                    danmaku_blocked_words = ?,
                    danmaku_duplicate_window_minutes = ?,
                    danmaku_convert_top_bottom_to_scroll = ?,
                    disabled_metadata_provider_ids = ?,
                    metadata_douban_cookie = ?,
                    metadata_tmdb_api_key = ?,
                    metadata_tmdb_proxy_base_url = ?,
                    metadata_bangumi_access_token = ?,
                    network_proxy_mode = ?,
                    network_proxy_url = ?,
                    network_proxy_bypass_rules = ?,
                    network_proxy_rules = ?,
                    youtube_cookie_browser = ?,
                    youtube_max_height = ?,
                    youtube_video_codec = ?,
                    youtube_default_subtitle_lang = ?,
                    youtube_default_audio_lang = ?,
                    youtube_metadata_language = ?,
                    youtube_region = ?,
                    youtube_category_source_type = ?,
                    youtube_category_source_value = ?,
                    youtube_category_cache_json = ?,
                    youtube_category_cache_refreshed_at = ?,
                    youtube_category_cache_error = ?,
                    mpv_render_profile = ?,
                    mpv_cache_size_mb = ?,
                    mpv_hwdec_mode = ?,
                    mpv_network_timeout_seconds = ?,
                    mpv_default_readahead_secs = ?,
                    mpv_extra_options = ?,
                    playback_auto_switch_source_on_failure = ?,
                    bilibili_grouped_playlist_tree_enabled = ?,
                    m3u_proxy_segment_prefetch_size = ?,
                    m3u8_ad_filter_mode = ?,
                    last_path = ?,
                    last_active_window = ?,
                    last_playback_source = ?,
                    last_playback_source_key = ?,
                    last_playback_mode = ?,
                    last_playback_path = ?,
                    last_playback_vod_id = ?,
                    last_playback_clicked_vod_id = ?,
                    last_player_paused = ?,
                    player_volume = ?,
                    player_muted = ?,
                    player_wide_mode = ?,
                    player_log_visible = ?,
                    preferred_parse_key = ?,
                    preferred_danmaku_enabled = ?,
                    preferred_danmaku_line_count = ?,
                    preferred_danmaku_render_mode = ?,
                    preferred_danmaku_color_mode = ?,
                    preferred_danmaku_uniform_color = ?,
                    preferred_danmaku_position_preset = ?,
                    preferred_danmaku_scroll_speed = ?,
                    preferred_danmaku_font_size = ?,
                    preferred_danmaku_opacity = ?,
                    preferred_danmaku_outline_strength = ?,
                    main_window_geometry = ?,
                    player_window_geometry = ?,
                    player_main_splitter_state = ?,
                    browse_content_splitter_state = ?,
                    last_selected_tab = ?,
                    last_selected_category_tab = ?,
                    last_selected_category_id = ?,
                    builtin_tab_overrides_json = ?,
                    global_search_history = ?,
                    global_search_hot_source = ?,
                    ai_enabled = ?,
                    ai_base_url = ?,
                    ai_api_key = ?,
                    ai_chat_model = ?,
                    ai_request_timeout_seconds = ?,
                    ai_metadata_enrichment_enabled = ?,
                    ai_danmaku_enrichment_enabled = ?,
                    ai_episode_title_rewrite_enabled = ?,
                    ai_following_summary_enabled = ?,
                    following_episode_display_mode = ?,
                    following_episode_grid_columns = ?,
                    following_backend_enabled = ?,
                    following_backend_auto_subscribe = ?,
                    home_mode = ?,
                    dandan_base_url = ?,
                    bangumi_data_danmaku_enabled = ?,
                    subtitle_subdl_api_key = ?,
                    subtitle_assrt_token = ?,
                    subtitle_opensubtitles_api_key = ?,
                    subtitle_subsource_api_key = ?,
                    disabled_subtitle_provider_ids = ?
                WHERE id = 1
                """,
                (
                    config.base_url,
                    config.username,
                    config.token,
                    config.vod_token,
                    _normalize_theme_mode(config.theme_mode),
                    int(config.logging_enabled),
                    int(config.metadata_enhancement_enabled),
                    int(config.episode_title_enhancement_enabled),
                    json.dumps(
                        _normalize_disabled_provider_ids(
                            config.disabled_danmaku_provider_ids,
                            VALID_DANMAKU_PROVIDER_IDS,
                        ),
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        _normalize_danmaku_blocked_words(
                            config.danmaku_blocked_words
                        ),
                        ensure_ascii=False,
                    ),
                    _normalize_danmaku_duplicate_window_minutes(
                        config.danmaku_duplicate_window_minutes
                    ),
                    int(config.danmaku_convert_top_bottom_to_scroll),
                    json.dumps(
                        _normalize_disabled_provider_ids(
                            config.disabled_metadata_provider_ids,
                            VALID_METADATA_PROVIDER_IDS,
                        ),
                        ensure_ascii=False,
                    ),
                    str(config.metadata_douban_cookie or "").strip(),
                    str(config.metadata_tmdb_api_key or "").strip(),
                    _normalize_tmdb_proxy_base_url(config.metadata_tmdb_proxy_base_url),
                    str(config.metadata_bangumi_access_token or "").strip(),
                    _normalize_network_proxy_mode(config.network_proxy_mode),
                    _normalize_network_proxy_url(config.network_proxy_url),
                    json.dumps(_normalize_network_proxy_bypass_rules(config.network_proxy_bypass_rules), ensure_ascii=False),
                    json.dumps(_normalize_network_proxy_rules(config.network_proxy_rules), ensure_ascii=False),
                    _normalize_youtube_cookie_browser(config.youtube_cookie_browser),
                    _normalize_youtube_max_height(config.youtube_max_height),
                    _normalize_youtube_video_codec(config.youtube_video_codec),
                    _normalize_youtube_subtitle_lang(config.youtube_default_subtitle_lang),
                    _normalize_youtube_audio_lang(config.youtube_default_audio_lang),
                    _normalize_youtube_metadata_language(config.youtube_metadata_language),
                    _normalize_youtube_region(config.youtube_region),
                    _normalize_youtube_category_source_type(config.youtube_category_source_type),
                    _normalize_youtube_category_source_value(config.youtube_category_source_value),
                    str(config.youtube_category_cache_json or ""),
                    _normalize_youtube_category_cache_refreshed_at(config.youtube_category_cache_refreshed_at),
                    str(config.youtube_category_cache_error or "").strip(),
                    _normalize_mpv_render_profile(
                        getattr(config, "mpv_render_profile", "auto"),
                        config.mpv_hwdec_mode,
                    ),
                    _normalize_mpv_cache_size_mb(config.mpv_cache_size_mb),
                    _normalize_mpv_hwdec_mode(config.mpv_hwdec_mode),
                    _normalize_mpv_network_timeout_seconds(config.mpv_network_timeout_seconds),
                    _normalize_mpv_default_readahead_secs(config.mpv_default_readahead_secs),
                    _normalize_mpv_extra_options(config.mpv_extra_options),
                    int(config.playback_auto_switch_source_on_failure),
                    int(config.bilibili_grouped_playlist_tree_enabled),
                    _normalize_m3u_proxy_segment_prefetch_size(config.m3u_proxy_segment_prefetch_size),
                    _normalize_m3u8_ad_filter_mode(config.m3u8_ad_filter_mode),
                    config.last_path,
                    config.last_active_window,
                    config.last_playback_source,
                    config.last_playback_source_key,
                    config.last_playback_mode,
                    config.last_playback_path,
                    config.last_playback_vod_id,
                    config.last_playback_clicked_vod_id,
                    int(config.last_player_paused),
                    config.player_volume,
                    int(config.player_muted),
                    int(config.player_wide_mode),
                    int(config.player_log_visible),
                    config.preferred_parse_key,
                    int(config.preferred_danmaku_enabled),
                    _normalize_danmaku_line_count(config.preferred_danmaku_line_count),
                    _normalize_danmaku_render_mode(config.preferred_danmaku_render_mode),
                    _normalize_danmaku_color_mode(config.preferred_danmaku_color_mode),
                    _normalize_danmaku_uniform_color(config.preferred_danmaku_uniform_color),
                    _normalize_danmaku_position_preset(config.preferred_danmaku_position_preset),
                    _normalize_danmaku_scroll_speed(config.preferred_danmaku_scroll_speed),
                    _normalize_danmaku_font_size(config.preferred_danmaku_font_size),
                    _normalize_danmaku_opacity(config.preferred_danmaku_opacity),
                    _normalize_danmaku_outline_strength(config.preferred_danmaku_outline_strength),
                    config.main_window_geometry,
                    config.player_window_geometry,
                    config.player_main_splitter_state,
                    config.browse_content_splitter_state,
                    config.last_selected_tab,
                    config.last_selected_category_tab,
                    config.last_selected_category_id,
                    str(getattr(config, "builtin_tab_overrides_json", "") or "").strip(),
                    json.dumps(_normalize_global_search_history(config.global_search_history), ensure_ascii=False),
                    str(config.global_search_hot_source or "360").strip() or "360",
                    int(config.ai_enabled),
                    _normalize_ai_base_url(config.ai_base_url),
                    _normalize_ai_secret(config.ai_api_key),
                    _normalize_ai_model(config.ai_chat_model),
                    _normalize_ai_timeout(config.ai_request_timeout_seconds),
                    int(config.ai_metadata_enrichment_enabled),
                    int(config.ai_danmaku_enrichment_enabled),
                    int(config.ai_episode_title_rewrite_enabled),
                    int(config.ai_following_summary_enabled),
                    _normalize_following_episode_display_mode(config.following_episode_display_mode),
                    _normalize_following_episode_grid_columns(
                        config.following_episode_grid_columns
                    ),
                    int(config.following_backend_enabled),
                    int(config.following_backend_auto_subscribe),
                    _normalize_home_mode(config.home_mode),
                    str(config.dandan_base_url or "").strip(),
                    int(config.bangumi_data_danmaku_enabled),
                    str(config.subtitle_subdl_api_key or "").strip(),
                    str(config.subtitle_assrt_token or "").strip(),
                    str(config.subtitle_opensubtitles_api_key or "").strip(),
                    str(config.subtitle_subsource_api_key or "").strip(),
                    json.dumps(
                        _normalize_disabled_provider_ids(
                            config.disabled_subtitle_provider_ids,
                            VALID_SUBTITLE_PROVIDER_IDS,
                        ),
                        ensure_ascii=False,
                    ),
                ),
            )

    def clear_token(self) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE app_config SET token = '', vod_token = '' WHERE id = 1")
