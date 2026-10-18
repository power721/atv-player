from __future__ import annotations

import re

_LEADING_MEDIA_PREFIX_RE = re.compile(r"^(?:电视剧|电影|剧集|综艺|动漫|动画|番剧|纪录片)\s*[:：]\s*", re.IGNORECASE)
_EMBEDDED_YEAR_SUFFIX_RE = re.compile(r"^(.*?[\(（]\s*(?:19|20)\d{2}\s*[\)）])(?:\s*.*)?$")
_TRAILING_BRACKET_NOISE_RE = re.compile(r"(?:\s*[【\[].*?[】\]])+$")
_TRAILING_PARENTHESIZED_QUALITY_RE = re.compile(
    r"(?:\s*[\(（]\s*(?:臻彩|真彩|4K|HDR|高码率|超清|高清|蓝光)\s*[\)）])+$",
    re.IGNORECASE,
)
_TRAILING_RELEASE_NOISE_RE = re.compile(
    r"(?:\s*(?:更新\d+集|更新至\d+集|更\d+集|第\d+集|4K(?:HDR\d*|HDR|60FPS)?|高码率|内嵌简中|内封简中|简中内嵌|简中|剧情|动画|动漫|番剧|剧集|电视剧|奇幻|冒险))+$",
    re.IGNORECASE,
)
_TRAILING_YEAR_RE = re.compile(r"(.*?)[\s]*[\(（]\s*((?:19|20)\d{2})\s*[\)）]\s*$")
_PURE_SEASON_TITLE_RE = re.compile(
    r"^(?:s\s?\d{1,2}|第\s*[0-9零一二两三四五六七八九十百]+\s*季|season\s*\d{1,2})$",
    re.IGNORECASE,
)


def is_pure_season_title(value: object) -> bool:
    """整个标题就是一个季标记("S01"/"第一季"/"Season 2"),不含任何剧名。"""
    return _PURE_SEASON_TITLE_RE.fullmatch(str(value or "").strip()) is not None
_TITLE_CATEGORY_PREFIX_RE = re.compile(r"^(电视剧|电影|剧集|连续剧|动漫|动画|番剧)\s*[:：]\s*", re.IGNORECASE)
_TITLE_CATEGORY_SUFFIX_RE = re.compile(r"(电视剧|电影|剧集|连续剧|剧版|真人版|动漫|动画|番剧)\s*$", re.IGNORECASE)

_TITLE_CATEGORY_HINTS = {
    "动漫": "动漫",
    "动画": "动漫",
    "番剧": "动漫",
    "电视剧": "剧集",
    "剧集": "剧集",
    "连续剧": "剧集",
    "剧版": "剧集",
    "真人版": "剧集",
    "电影": "电影",
}


def is_short_drama_collection(*values: object) -> bool:
    """Return whether the supplied media labels identify a short-drama collection.

    Short-drama update directories are not useful metadata/danmaku lookup targets:
    their titles usually describe a daily catalogue rather than a real programme.
    Keep the title-only check conservative so a normal drama whose name happens to
    contain ``短剧`` is not silently excluded unless it is explicitly categorised as
    short drama or clearly names a catalogue/collection.
    """
    texts = [str(value or "").strip() for value in values if str(value or "").strip()]
    if not texts:
        return False
    explicit_labels = texts[1:]
    if any("短剧" in text or "短片" in text for text in explicit_labels):
        return True
    collection_markers = ("目录", "合集", "更新")
    return any(
        "短剧" in text and any(marker in text for marker in collection_markers)
        for text in texts
    )


def infer_metadata_category_name_from_title(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    prefix_match = _TITLE_CATEGORY_PREFIX_RE.match(text)
    if prefix_match is not None:
        return _TITLE_CATEGORY_HINTS.get(prefix_match.group(1), "")
    suffix_match = _TITLE_CATEGORY_SUFFIX_RE.search(text)
    if suffix_match is not None:
        return _TITLE_CATEGORY_HINTS.get(suffix_match.group(1), "")
    return ""


def normalize_metadata_title(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"^[#＃]+\s*", "", text).strip()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.sub(r"^[^\w\u4e00-\u9fff]+", "", normalized).strip()
    normalized = _LEADING_MEDIA_PREFIX_RE.sub("", normalized).strip()

    year_match = _EMBEDDED_YEAR_SUFFIX_RE.match(normalized)
    if year_match is not None:
        return year_match.group(1).strip()

    normalized = _TRAILING_BRACKET_NOISE_RE.sub("", normalized).strip()
    normalized = _TRAILING_PARENTHESIZED_QUALITY_RE.sub("", normalized).strip()
    normalized = _TRAILING_RELEASE_NOISE_RE.sub("", normalized).strip()
    return normalized or text


def normalize_metadata_query_inputs(title: object, year: object) -> tuple[str, str]:
    normalized_title = normalize_metadata_title(title)
    normalized_year = str(year or "").strip()
    year_match = _TRAILING_YEAR_RE.fullmatch(normalized_title)
    if year_match is not None:
        embedded_title = year_match.group(1).strip()
        embedded_year = year_match.group(2).strip()
        if embedded_title:
            normalized_title = embedded_title
        # Prefer an explicit year embedded in the title over a conflicting external year.
        normalized_year = embedded_year
    return normalized_title.strip(), normalized_year
