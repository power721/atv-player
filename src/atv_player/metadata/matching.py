from __future__ import annotations

from difflib import SequenceMatcher
import re
from typing import TYPE_CHECKING

from atv_player.episode_titles import extract_season_number
from atv_player.metadata.models import MetadataMatch, MetadataQuery
from atv_player.metadata.query import is_pure_season_title

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_MIN_CONFIDENT_MATCH_SCORE = 0.45
# A match whose title merely *extends* the query (e.g. a "剧情揭秘" companion special
# for the real show) is a different work. Score it below confidence so corroborating
# signals (year/area/actors) are required to promote it.
_COMPANION_TITLE_SIMILARITY_SCORE = 0.35
_CATEGORY_MATCH_BONUS = 0.12
_CATEGORY_MISMATCH_PENALTY = 0.08
_AREA_MATCH_BONUS = 0.03
_LANGUAGE_MATCH_BONUS = 0.03
_DIRECTOR_MATCH_BONUS = 0.05
_ACTOR_MATCH_BONUS = 0.04

_CATEGORY_SYNONYMS = {
    "动漫": {"动漫", "动画", "番剧", "anime"},
    "动画": {"动漫", "动画", "番剧", "anime"},
    "番剧": {"动漫", "动画", "番剧", "anime"},
    "电视剧": {"电视剧", "剧集", "连续剧", "tv"},
    "剧集": {"电视剧", "剧集", "连续剧", "tv"},
    "连续剧": {"电视剧", "剧集", "连续剧", "tv"},
    "电影": {"电影", "影片", "movie"},
    "影片": {"电影", "影片", "movie"},
    "少儿": {"少儿", "儿童"},
    "儿童": {"少儿", "儿童"},
    "纪录片": {"纪录片", "纪录"},
    "纪录": {"纪录片", "纪录"},
}


def normalize_match_title(value: object) -> str:
    return re.sub(r"[\s\-_:.：,，/\\|·•'\"`()（）《》【】\[\]]+", "", str(value or "").strip().lower())


def strip_match_season_suffix(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    stripped = re.sub(
        r"(?:\s*[-:：]\s*)?(?:第\s*[0-9零一二两三四五六七八九十百千]+\s*季|season\s*\d+|s\d+)\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    return stripped or text


def match_season_number(match: MetadataMatch) -> int | None:
    raw_season = match.raw.get("season_number")
    if isinstance(raw_season, int) and raw_season > 0:
        return raw_season
    provider_id = str(match.provider_id or "").strip()
    provider_id_match = re.search(r":season:(\d+)$", provider_id)
    if provider_id_match is not None:
        try:
            season_number = int(provider_id_match.group(1))
        except (TypeError, ValueError):
            season_number = 0
        if season_number > 0:
            return season_number
    return extract_season_number(match.title)


def is_confident_match(score: float) -> bool:
    return score >= _MIN_CONFIDENT_MATCH_SCORE


def score_match(query: MetadataQuery, match: MetadataMatch) -> float:
    explicit_score = float(match.score or 0.0)
    title_score = _title_similarity_score(query.title, match.title)
    score = max(explicit_score, title_score)

    query_season = extract_season_number(query.title)
    match_season = match_season_number(match)
    if query_season is not None:
        if match_season == query_season:
            score += 0.2
        elif match_season is not None:
            score -= 0.25

    query_year = str(query.year or "").strip()
    match_year = str(match.year or "").strip()
    if query_year and match_year:
        if query_year == match_year:
            score += 0.05
        else:
            query_year_number = _parse_year(query_year)
            match_year_number = _parse_year(match_year)
            if (
                query_year_number is not None
                and match_year_number is not None
                and abs(query_year_number - match_year_number) >= 2
                and query_season is None
            ):
                score -= 0.7
            else:
                score -= 0.08

    score += _category_score(query.category_name, query.type_name, match.raw)
    score += _original_detail_field_score(query, match.raw)

    if _is_full_exact_match(query.title, match.title):
        if match.provider == "bilibili":
            score += 0.3
        elif match.provider == "iqiyi":
            score += 0.15
        elif match.provider == "tencent":
            score += 0.2
    return score


def _title_similarity_score(query_title: str, match_title: str) -> float:
    normalized_query = normalize_match_title(query_title)
    normalized_match = normalize_match_title(match_title)
    if not normalized_query or not normalized_match:
        return 0.0
    if normalized_query == normalized_match:
        return 1.0
    if is_pure_season_title(query_title):
        # 纯季标记("S01")会被任何恰含该字样的标题"包含"(如优酷别名注记
        # "…(别名:Garfield S01)"),包含规则在这里不是证据;乘 0.4 压到
        # 置信线以下,除非两个标题整串相等(上面已返回 1.0)。
        return SequenceMatcher(a=normalized_query, b=normalized_match).ratio() * 0.4

    query_base = normalize_match_title(strip_match_season_suffix(query_title))
    match_base = normalize_match_title(strip_match_season_suffix(match_title))
    if query_base and query_base == match_base:
        return 0.78
    if query_base and match_base:
        if match_base in query_base:
            # Match is the canonical base show; the query adds a suffix/variant.
            return 0.65
        if query_base in match_base:
            # Match adds content beyond the query → likely a companion/derivative work.
            return _COMPANION_TITLE_SIMILARITY_SCORE
    return SequenceMatcher(a=query_base or normalized_query, b=match_base or normalized_match).ratio() * 0.6


def _parse_year(value: object) -> int | None:
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    if match is None:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _is_full_exact_match(query_title: str, match_title: str) -> bool:
    return normalize_match_title(query_title) == normalize_match_title(match_title)


def _category_score(query_category_name: str, query_type_name: str, raw: Mapping[str, object]) -> float:
    query_categories = _expanded_categories(
        [
            *_category_tokens(query_category_name),
            *_category_tokens(query_type_name),
        ]
    )
    if not query_categories:
        return 0.0
    match_categories = _expanded_categories(_raw_category_tokens(raw))
    if not match_categories:
        return 0.0
    if query_categories & match_categories:
        return _CATEGORY_MATCH_BONUS
    return -_CATEGORY_MISMATCH_PENALTY


def _original_detail_field_score(query: MetadataQuery, raw: Mapping[str, object]) -> float:
    score = 0.0
    if _text_value_tokens(query.vod_area) & _raw_text_tokens(raw, "country", "region", "areas", "area"):
        score += _AREA_MATCH_BONUS
    if _text_value_tokens(query.vod_lang) & _raw_text_tokens(raw, "language", "lang"):
        score += _LANGUAGE_MATCH_BONUS
    if _person_tokens(query.vod_director) & _raw_people_tokens(raw, "directors", "director"):
        score += _DIRECTOR_MATCH_BONUS
    if _person_tokens(query.vod_actor) & _raw_people_tokens(raw, "actors", "actor", "cv"):
        score += _ACTOR_MATCH_BONUS
    return score


def _expanded_categories(values: Iterable[str]) -> set[str]:
    expanded: set[str] = set()
    for value in values:
        normalized = normalize_match_title(value)
        if not normalized:
            continue
        expanded.add(normalized)
        expanded.update(normalize_match_title(alias) for alias in _CATEGORY_SYNONYMS.get(value, set()))
        expanded.update(normalize_match_title(alias) for alias in _CATEGORY_SYNONYMS.get(normalized, set()))
    return {value for value in expanded if value}


def _raw_category_tokens(raw: Mapping[str, object]) -> list[str]:
    values: list[str] = []
    values.extend(_category_tokens(raw.get("typeName")))
    values.extend(_category_tokens(raw.get("channel")))
    values.extend(_category_tokens(raw.get("genres")))
    values.extend(_category_tokens(raw.get("categories")))
    values.extend(_category_tokens(raw.get("baseTags")))

    category = raw.get("category")
    if isinstance(category, dict):
        values.extend(_category_tokens(category.get("value")))
    else:
        values.extend(_category_tokens(category))
    return values


def _raw_text_tokens(raw: Mapping[str, object], *keys: str) -> set[str]:
    tokens: set[str] = set()
    for key in keys:
        tokens.update(_text_value_tokens(raw.get(key)))
    return tokens


def _raw_people_tokens(raw: Mapping[str, object], *keys: str) -> set[str]:
    tokens: set[str] = set()
    for key in keys:
        tokens.update(_person_tokens(raw.get(key)))
    return tokens


def _text_value_tokens(value: object) -> set[str]:
    return {
        normalized
        for token in _category_tokens(value)
        if (normalized := normalize_match_title(token))
    }


def _person_tokens(value: object) -> set[str]:
    return _text_value_tokens(value)


def _category_tokens(value: object) -> list[str]:
    if isinstance(value, dict):
        return _category_tokens(value.get("value"))
    if isinstance(value, list):
        tokens: list[str] = []
        for item in value:
            tokens.extend(_category_tokens(item))
        return tokens
    text = str(value or "").strip()
    if not text:
        return []
    candidates = re.split(r"[,/|、]", text)
    return [
        token
        for token in (candidate.strip() for candidate in candidates)
        if token and not token.isdigit()
    ]
