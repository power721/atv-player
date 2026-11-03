from __future__ import annotations

from dataclasses import replace
import re

from atv_player.danmaku.utils import (
    _extract_variety_date_key,
    extract_variety_part,
    infer_playlist_episode_number,
    is_likely_variety_title,
    is_variety_collection,
)
from atv_player.episode_titles import (
    apply_episode_title_index_map,
    episode_version_slots_by_index,
    extract_season_number,
    playlist_has_title_variants,
    seed_original_titles,
)
from atv_player.metadata.models import MetadataQuery
from atv_player.metadata.query import infer_metadata_category_name_from_title, normalize_metadata_title
from atv_player.metadata.providers.tmdb import infer_tmdb_media_type
from atv_player.metadata.season_alignment import extract_playlist_item_seasons
from atv_player.models import PlayItem, VodItem

METADATA_EPISODE_TITLE_SOURCE_PRIORITY = ["manual", "plugin", "bangumi", "bilibili", "tmdb", "tencent", "iqiyi"]
_IQIYI_PRIORITIZED_EPISODE_TITLE_SOURCE_PRIORITY = ["manual", "plugin", "bangumi", "bilibili", "iqiyi", "tmdb", "tencent"]
# Variety shows: the official native source (Tencent/iQiyi) carries the real,
# date-ordered episode list that TMDB's flat season/episode model cannot represent,
# so it must outrank TMDB.
_VARIETY_EPISODE_TITLE_SOURCE_PRIORITY = ["manual", "plugin", "tencent", "iqiyi", "bilibili", "bangumi", "tmdb"]
_MOVIE_MARKERS = ("电影", "影片", "movie")
_ANIME_MARKERS = ("动漫", "动画", "番剧", "anime", "animation", "国创")
_LIVE_ACTION_MARKERS = ("电视剧", "剧集", "连续剧", "剧版", "真人版", "真人", "短剧")
_EPISODE_SORT_SENTINEL = 10**9
_VARIETY_PUBLISH_DATE_RE = re.compile(r"(\d{4})\D(\d{1,2})\D(\d{1,2})")


def is_high_confidence_iqiyi_episode_candidate(
    vod: VodItem,
    playlist: list[PlayItem],
    candidate,
    *,
    preferred_provider: str = "",
) -> bool:
    provider = str(getattr(candidate, "provider", "") or "").strip()
    if provider != "iqiyi":
        return False
    native_iqiyi_site = _is_native_iqiyi_site_candidate(candidate)
    if (
        str(preferred_provider or "").strip() != "iqiyi"
        and not native_iqiyi_site
        and not _iqiyi_titles_match_vod(vod, candidate)
    ):
        return False
    return (
        build_provider_episode_playlist(
            vod,
            playlist,
            candidate,
            source_priority=METADATA_EPISODE_TITLE_SOURCE_PRIORITY,
        )
        is not None
    )


def resolve_episode_title_source_priority(
    vod: VodItem,
    playlist: list[PlayItem],
    candidates: list[object],
    *,
    preferred_provider: str = "",
) -> list[str]:
    if _playlist_is_variety(vod, playlist):
        return list(_VARIETY_EPISODE_TITLE_SOURCE_PRIORITY)
    for candidate in candidates:
        if is_high_confidence_iqiyi_episode_candidate(
            vod,
            playlist,
            candidate,
            preferred_provider=preferred_provider,
        ):
            return list(_IQIYI_PRIORITIZED_EPISODE_TITLE_SOURCE_PRIORITY)
    return list(METADATA_EPISODE_TITLE_SOURCE_PRIORITY)


def build_provider_episode_playlist(
    vod: VodItem,
    playlist: list[PlayItem],
    candidate,
    *,
    source_priority: list[str],
) -> list[PlayItem] | None:
    provider = str(getattr(candidate, "provider", "") or "").strip()
    provider_id = str(getattr(candidate, "provider_id", "") or "").strip()
    raw = dict(getattr(candidate, "raw", {}) or {})
    rewrite_supported = _candidate_supports_episode_title_rewrite(
        vod, provider, provider_id, raw, playlist=playlist
    )
    if not rewrite_supported:
        return None
    copied = seed_original_titles([replace(item) for item in playlist])
    titles_by_index = _titles_by_index_for_provider(vod, copied, provider, raw)
    if not titles_by_index:
        return None
    apply_episode_title_index_map(copied, titles_by_index, source=provider, source_priority=source_priority)
    if not playlist_has_title_variants(copied):
        return None
    return _sort_episode_title_playlist(vod, copied)


def _candidate_season_number(
    provider: str,
    provider_id: str,
    raw: dict[str, object],
) -> int | None:
    if provider == "tmdb":
        segments = provider_id.split(":")
        if len(segments) >= 4 and segments[2] == "season":
            try:
                return int(segments[3])
            except (TypeError, ValueError):
                return None
        raw_season = raw.get("season_number")
        try:
            return int(raw_season) if raw_season else None
        except (TypeError, ValueError):
            return None
    return None


def _candidate_supports_episode_title_rewrite(
    vod: VodItem,
    provider: str,
    provider_id: str,
    raw: dict[str, object],
    *,
    playlist: list[PlayItem] | None = None,
) -> bool:
    vod_media_type = infer_tmdb_media_type(
        MetadataQuery(
            title=str(vod.vod_name or "").strip(),
            year=str(vod.vod_year or "").strip(),
            type_name=str(vod.type_name or "").strip(),
            category_name=str(vod.category_name or "").strip(),
        )
    )
    if vod_media_type == "movie":
        return False
    if provider == "tmdb" and not provider_id.startswith("tv:"):
        return False
    vod_kind = _vod_media_kind(vod)
    candidate_kind = _candidate_media_kind(provider, provider_id, raw)
    if vod_kind and candidate_kind and vod_kind != candidate_kind:
        return False
    if provider == "bilibili" and not _is_confirmed_bilibili_anime_candidate(raw):
        return False
    if playlist:
        # 跨季硬拒绝:文件自身声明了别的季(S01E05 ≠ S02E05),候选季不同
        # 时不做标题重写,防进度/标题错季。
        candidate_season = _candidate_season_number(provider, provider_id, raw)
        if candidate_season is not None:
            known_seasons = {
                season
                for season in extract_playlist_item_seasons(playlist)
                if season is not None
            }
            if known_seasons and candidate_season not in known_seasons:
                return False
    return not _raw_indicates_movie_category(raw)


def _vod_media_kind(vod: VodItem) -> str:
    return _classify_media_kind(
        getattr(vod, "category_name", ""),
        getattr(vod, "type_name", ""),
        infer_metadata_category_name_from_title(getattr(vod, "vod_name", "")),
        getattr(vod, "vod_name", ""),
    )


def _candidate_media_kind(provider: str, provider_id: str, raw: dict[str, object]) -> str:
    if provider == "bangumi":
        categories = raw.get("categories") or []
        if isinstance(categories, list) and any(
            any(marker in str(category).strip().lower() for marker in _ANIME_MARKERS)
            for category in categories
        ):
            return "anime"
        return ""
    if provider == "tmdb" and provider_id.startswith("movie:"):
        return "movie"
    return _classify_media_kind(
        raw.get("typeName"),
        raw.get("channel"),
        raw.get("genres"),
        raw.get("categories"),
        raw.get("baseTags"),
        raw.get("category"),
    )


def _classify_media_kind(*values: object) -> str:
    text = " ".join(_iter_media_kind_tokens(*values)).lower()
    if not text:
        return ""
    if any(marker in text for marker in _ANIME_MARKERS):
        return "anime"
    if any(marker in text for marker in _MOVIE_MARKERS):
        return "movie"
    if any(marker in text for marker in _LIVE_ACTION_MARKERS):
        return "live_action"
    return ""


def _iter_media_kind_tokens(*values: object) -> list[str]:
    tokens: list[str] = []
    for value in values:
        if isinstance(value, dict):
            tokens.extend(_iter_media_kind_tokens(value.get("value")))
            continue
        if isinstance(value, list):
            for item in value:
                tokens.extend(_iter_media_kind_tokens(item))
            continue
        text = str(value or "").strip()
        if text:
            tokens.append(text)
    return tokens


def _is_confirmed_bilibili_anime_candidate(raw: dict[str, object]) -> bool:
    season_id = str(raw.get("season_id") or "").strip()
    episodes = raw.get("episodes")
    return bool(season_id and isinstance(episodes, list) and episodes)


def _raw_indicates_movie_category(raw: dict[str, object]) -> bool:
    return any(marker in token for token in _iter_category_tokens(raw) for marker in _MOVIE_MARKERS)


def _iter_category_tokens(raw: dict[str, object]) -> list[str]:
    values: list[str] = []
    for key in ("typeName", "channel", "genres", "categories", "baseTags", "category"):
        values.extend(_category_tokens(raw.get(key)))
    return values


def _category_tokens(value: object) -> list[str]:
    if isinstance(value, dict):
        return _category_tokens(value.get("value"))
    if isinstance(value, list):
        tokens: list[str] = []
        for item in value:
            tokens.extend(_category_tokens(item))
        return tokens
    text = str(value or "").strip().lower()
    if not text:
        return []
    return [token for token in re.split(r"[,/|、]", text) if token.strip()]


def _titles_by_index_for_provider(
    vod: VodItem,
    playlist: list[PlayItem],
    provider: str,
    raw: dict[str, object],
) -> dict[int, str]:
    if _playlist_is_variety(vod, playlist):
        # Variety episodes are keyed by air date + 期段, not sequential episode
        # numbers. The collapsed-number path (tmdb/bangumi/bilibili/iqiyi) maps
        # 期上/中/下/加更 to a single episode and would scramble 纯享/陪看/花絮
        # onto unrelated episodes (e.g. 第1期上纯享 -> iqiyi ep1). Only the
        # official date-keyed matcher may emit; everything else stays unmapped.
        if provider == "tencent" and _is_tencent_variety_candidate(vod, playlist, raw):
            return _titles_by_index_for_tencent_variety(vod, playlist, raw)
        return {}
    if provider == "bangumi":
        return _titles_by_index_for_bangumi(vod, playlist, raw)
    if provider == "tencent":
        return _titles_by_index_for_tencent(vod, playlist, raw)
    if provider == "iqiyi":
        return _titles_by_index_for_iqiyi(vod, playlist, raw)
    if provider == "bilibili":
        return _titles_by_index_for_bilibili(vod, playlist, raw)
    if provider == "tmdb":
        return _titles_by_index_for_tmdb(vod, playlist, raw)
    return {}


def _playlist_is_variety(vod: VodItem, playlist: list[PlayItem]) -> bool:
    if is_variety_collection(
        getattr(vod, "type_name", ""),
        getattr(vod, "category_name", ""),
        getattr(vod, "vod_tag", ""),
        getattr(vod, "vod_content", ""),
    ):
        return True
    if not playlist:
        return False
    variety_items = sum(
        is_likely_variety_title(item.original_title or item.title or "") for item in playlist
    )
    return variety_items >= 2 and variety_items * 2 >= len(playlist)


def _variety_date_key_from_publish_date(publish_date: object) -> str:
    match = _VARIETY_PUBLISH_DATE_RE.search(str(publish_date or ""))
    if match is None:
        return ""
    year, month, day = match.groups()
    return f"{year}{int(month):02d}{int(day):02d}"


def _is_tencent_variety_candidate(
    vod: VodItem,
    playlist: list[PlayItem],
    raw: dict[str, object],
) -> bool:
    """A Tencent candidate whose hydrated cover list can be matched by air date."""
    if not _playlist_is_variety(vod, playlist):
        return False
    episodes = raw.get("episodes")
    if not isinstance(episodes, list):
        return False
    return any(
        isinstance(episode, dict) and str(episode.get("publish_date") or "").strip()
        for episode in episodes
    )


def _titles_by_index_for_tencent_variety(
    vod: VodItem,
    playlist: list[PlayItem],
    raw: dict[str, object],
) -> dict[int, str]:
    """Match variety files to the official Tencent episode list by (air date, part).

    Air date is the strongest signal (filenames carry a YYYYMMDD prefix); the
    期上/中/下/加更 part disambiguates same-date halves. Same-date files/episodes
    without a parseable part (e.g. 先导片上/下) are paired in broadcast order.
    Output is formatted ``MM-DD {官方标题}`` (no meaningless 第N集 prefix).
    """
    official_by_date: dict[str, list[dict[str, object]]] = {}
    for episode in raw.get("episodes") or []:
        if not isinstance(episode, dict):
            continue
        title = str(episode.get("title") or "").strip()
        date_key = _variety_date_key_from_publish_date(episode.get("publish_date"))
        if not title or not date_key:
            continue
        official_by_date.setdefault(date_key, []).append(
            {
                "title": title,
                "part": extract_variety_part(title),
                "mmdd": f"{int(date_key[4:6]):02d}-{int(date_key[6:8]):02d}",
            }
        )
    if not official_by_date:
        return {}

    files_by_date: dict[str, list[tuple[int, str | None]]] = {}
    for index, item in enumerate(playlist):
        name = str(item.original_title or item.title or item.path or "")
        date_key = _extract_variety_date_key(name)
        if not date_key:
            continue
        files_by_date.setdefault(date_key, []).append((index, extract_variety_part(name)))

    titles_by_index: dict[int, str] = {}
    for date_key, files in files_by_date.items():
        officials = official_by_date.get(date_key)
        if not officials:
            continue
        used: set[int] = set()
        # 1) exact (date, part) match — handles 第N期上/中/下/加更.
        for index, file_part in files:
            if file_part is None:
                continue
            for position, official in enumerate(officials):
                if position in used:
                    continue
                if official["part"] is not None and official["part"] == file_part:
                    titles_by_index[index] = f'{official["mmdd"]} {official["title"]}'
                    used.add(position)
                    break
        # 2) order fallback for same-date items without a part (e.g. 先导片上/下).
        remaining_officials = [official for position, official in enumerate(officials) if position not in used]
        remaining_indices = [index for index, _part in files if index not in titles_by_index]
        for index, official in zip(remaining_indices, remaining_officials):
            titles_by_index[index] = f'{official["mmdd"]} {official["title"]}'
    return titles_by_index


def _titles_by_index_for_tencent(vod: VodItem, playlist: list[PlayItem], raw: dict[str, object]) -> dict[int, str]:
    episode_rows: list[str] = []
    for site_key in ("episode_sites", "play_sites"):
        for site in raw.get(site_key) or []:
            if not isinstance(site, dict):
                continue
            for episode in site.get("episodeInfoList") or []:
                if not isinstance(episode, dict):
                    continue
                title = str(episode.get("title") or "").strip()
                if title:
                    episode_rows.append(title)
    if not episode_rows:
        return {}
    return _map_sequential_episode_rows(vod, playlist, episode_rows)


def _titles_by_index_for_iqiyi(vod: VodItem, playlist: list[PlayItem], raw: dict[str, object]) -> dict[int, str]:
    titles_by_episode: dict[int, str] = {}
    for video in raw.get("videos") or []:
        if not isinstance(video, dict):
            continue
        try:
            episode_number = int(video.get("itemNumber") or video.get("episodeNumber") or video.get("number") or 0)
        except (TypeError, ValueError):
            continue
        episode_title = str(video.get("itemTitle") or video.get("subtitle") or video.get("title") or "").strip()
        if episode_number > 0 and episode_title:
            titles_by_episode[episode_number] = episode_title
    if not titles_by_episode:
        return {}
    return _map_episode_numbers_to_indices(vod, playlist, titles_by_episode)


def _titles_by_index_for_bangumi(vod: VodItem, playlist: list[PlayItem], raw: dict[str, object]) -> dict[int, str]:
    titles_by_episode: dict[int, str] = {}
    for episode in raw.get("episodes") or []:
        if not isinstance(episode, dict):
            continue
        try:
            episode_type = int(episode.get("type") or 0)
            episode_number = int(episode.get("sort") or episode.get("ep") or 0)
        except (TypeError, ValueError):
            continue
        episode_title = str(episode.get("name_cn") or episode.get("name") or "").strip()
        if episode_type != 0 or episode_number <= 0 or not episode_title:
            continue
        titles_by_episode[episode_number] = episode_title
    if not titles_by_episode:
        return {}
    return _map_episode_numbers_to_indices(vod, playlist, titles_by_episode)


def _titles_by_index_for_tmdb(vod: VodItem, playlist: list[PlayItem], raw: dict[str, object]) -> dict[int, str]:
    titles_by_episode: dict[int, str] = {}
    for episode in raw.get("episodes") or []:
        if not isinstance(episode, dict):
            continue
        try:
            episode_number = int(episode.get("episode_number") or episode.get("episodeNumber") or 0)
        except (TypeError, ValueError):
            continue
        episode_title = str(episode.get("name") or episode.get("title") or "").strip()
        if episode_number > 0 and episode_title:
            titles_by_episode[episode_number] = episode_title
    if not titles_by_episode:
        return {}
    return _map_episode_numbers_to_indices(vod, playlist, titles_by_episode)


def _titles_by_index_for_bilibili(vod: VodItem, playlist: list[PlayItem], raw: dict[str, object]) -> dict[int, str]:
    titles_by_episode: dict[int, str] = {}
    normalized_episodes = raw.get("episodes")
    if isinstance(normalized_episodes, list):
        for episode in normalized_episodes:
            if not isinstance(episode, dict):
                continue
            if str(episode.get("episode_type") or "main").strip() != "main":
                continue
            try:
                episode_number = int(episode.get("episode_number") or episode.get("sort") or 0)
            except (TypeError, ValueError):
                continue
            episode_title = str(episode.get("long_title") or episode.get("title") or "").strip()
            if episode_number > 0 and episode_title:
                titles_by_episode[episode_number] = episode_title
    if titles_by_episode:
        return _map_episode_numbers_to_indices(vod, playlist, titles_by_episode)
    for episode in raw.get("eps") or []:
        if not isinstance(episode, dict):
            continue
        episode_number = _bilibili_episode_number(episode)
        episode_title = _bilibili_episode_title(episode)
        if episode_number is None or episode_number <= 0 or not episode_title:
            continue
        titles_by_episode[episode_number] = episode_title
    if not titles_by_episode:
        return {}
    return _map_episode_numbers_to_indices(vod, playlist, titles_by_episode)


def _map_sequential_episode_rows(vod: VodItem, playlist: list[PlayItem], episode_rows: list[str]) -> dict[int, str]:
    titles_by_index: dict[int, str] = {}
    season_numbers = _resolved_season_numbers(vod, playlist)
    include_season_prefix = len(set(season_numbers.values())) > 1
    for index, item in enumerate(playlist):
        episode_number = infer_playlist_episode_number(item, playlist)
        if episode_number is None or episode_number <= 0 or episode_number > len(episode_rows):
            continue
        season_number = season_numbers[index]
        titles_by_index[index] = _format_episode_title(
            season_number,
            episode_number,
            episode_rows[episode_number - 1],
            include_season_prefix=include_season_prefix,
        )
    return titles_by_index


def _map_episode_numbers_to_indices(
    vod: VodItem,
    playlist: list[PlayItem],
    titles_by_episode: dict[int, str],
) -> dict[int, str]:
    titles_by_index: dict[int, str] = {}
    season_numbers = _resolved_season_numbers(vod, playlist)
    include_season_prefix = len(set(season_numbers.values())) > 1
    for index, item in enumerate(playlist):
        episode_number = infer_playlist_episode_number(item, playlist)
        if episode_number is None or episode_number <= 0:
            continue
        episode_title = titles_by_episode.get(episode_number)
        if not episode_title:
            continue
        season_number = season_numbers[index]
        titles_by_index[index] = _format_episode_title(
            season_number,
            episode_number,
            episode_title,
            include_season_prefix=include_season_prefix,
        )
    return titles_by_index


def _season_episode_pairs(vod: VodItem, playlist: list[PlayItem]) -> list[tuple[int, int] | None]:
    season_numbers = _resolved_season_numbers(vod, playlist)
    pairs: list[tuple[int, int] | None] = []
    for index, item in enumerate(playlist):
        episode_number = infer_playlist_episode_number(item, playlist)
        if episode_number is None or episode_number <= 0:
            pairs.append(None)
            continue
        season_number = season_numbers[index]
        # 排序专用:未知季按第 1 季排序保持历史展示顺序;对齐/前缀语义仍走
        # _resolved_season_numbers 的 None(未知≠第 1 季)。
        sort_season = season_number if season_number is not None else 1
        pairs.append((sort_season, episode_number))
    return pairs


def _sort_episode_title_playlist(vod: VodItem, playlist: list[PlayItem]) -> list[PlayItem]:
    if len(playlist) <= 1:
        for index, item in enumerate(playlist):
            item.index = index
        return playlist
    season_episode_pairs = _season_episode_pairs(vod, playlist)
    resolved_pairs = [pair for pair in season_episode_pairs if pair is not None]
    has_multi_version_pairs = len(resolved_pairs) != len(set(resolved_pairs))
    indexed_playlist = list(enumerate(playlist))
    if has_multi_version_pairs:
        version_slot_by_index = episode_version_slots_by_index(
            playlist,
            season_episode_pairs,
            sentinel=_EPISODE_SORT_SENTINEL,
        )
        indexed_playlist.sort(
            key=lambda entry: (
                version_slot_by_index[entry[0]],
                season_episode_pairs[entry[0]] or (_EPISODE_SORT_SENTINEL, _EPISODE_SORT_SENTINEL),
                entry[0],
            )
        )
    else:
        indexed_playlist.sort(
            key=lambda entry: (
                season_episode_pairs[entry[0]] or (_EPISODE_SORT_SENTINEL, _EPISODE_SORT_SENTINEL),
                entry[0],
            )
        )
    sorted_playlist = [item for _original_index, item in indexed_playlist]
    for index, item in enumerate(sorted_playlist):
        item.index = index
    return sorted_playlist


def _resolved_season_numbers(
    vod: VodItem,
    playlist: list[PlayItem],
) -> dict[int, int | None]:
    default_season = _guess_default_season(vod)
    resolved: dict[int, int | None] = {}
    for index, item in enumerate(playlist):
        season_number = None
        for value in (item.original_title, item.title, item.path):
            season_number = extract_season_number(value)
            if season_number is not None:
                break
        resolved[index] = season_number if season_number is not None else default_season
    return resolved


def _guess_default_season(vod: VodItem) -> int | None:
    """标题季标记;解析不出返回 None——"无法判断"不等于第 1 季。"""
    for value in (vod.vod_name, vod.vod_remarks, vod.category_name):
        season_number = extract_season_number(value)
        if season_number is not None:
            return season_number
    return None


def _iqiyi_titles_match_vod(vod: VodItem, candidate) -> bool:
    vod_title = normalize_metadata_title(str(vod.vod_name or "").strip())
    candidate_title = normalize_metadata_title(str(getattr(candidate, "title", "") or "").strip())
    if not vod_title or not candidate_title or vod_title != candidate_title:
        return False
    vod_year = str(vod.vod_year or "").strip()
    candidate_year = str(getattr(candidate, "year", "") or "").strip()
    if vod_year and candidate_year and vod_year != candidate_year:
        return False
    candidate_season = extract_season_number(getattr(candidate, "title", ""))
    vod_season = _guess_default_season(vod)
    if candidate_season is not None and vod_season is not None and candidate_season != vod_season:
        return False
    return True


def _is_native_iqiyi_site_candidate(candidate) -> bool:
    raw = dict(getattr(candidate, "raw", {}) or {})
    site_name = str(raw.get("siteName") or "").strip()
    site_id = str(raw.get("siteId") or "").strip().lower()
    return site_name == "爱奇艺" or site_id == "iqiyi"


def _bilibili_episode_number(episode: dict[str, object]) -> int | None:
    for key in ("title", "index_title"):
        try:
            value = int(str(episode.get(key) or "").strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _bilibili_episode_title(episode: dict[str, object]) -> str:
    for key in ("long_title", "share_copy", "show_title", "index_title", "title"):
        value = str(episode.get(key) or "").strip()
        if value:
            return value
    return ""


def _format_episode_title(
    season_number: int | None,
    episode_number: int,
    episode_title: str,
    *,
    include_season_prefix: bool,
) -> str:
    prefix = (
        f"第{season_number}季 "
        if include_season_prefix and season_number is not None
        else ""
    )
    return f"{prefix}第{episode_number}集 {episode_title}".strip()
