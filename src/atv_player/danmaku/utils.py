from __future__ import annotations

import re
from difflib import SequenceMatcher
from html import escape
from typing import Sequence
from urllib.parse import urlparse

from atv_player.danmaku.models import DanmakuRecord, DanmakuSourceSearchResult
from atv_player.models import PlayItem

_NOISE_PATTERNS = (
    r"【[^】]*】",
    r"\[[^\]]*\]",
    r"\([^)]*(高清|超清|蓝光|qq\.com|youku\.com)[^)]*\)",
)

_CN_NUM = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_EPISODE_PATTERNS = (
    r"第\s*([0-9零一二两三四五六七八九十百]+)\s*[集话期部回]",
    r"\s+0*([0-9]+)\s*[集话期]",
    r"(?<!\d)0*([0-9]+)\s*[集话期]",
    r"\s+0*([0-9]{1,4})\s*$",
    r"\bS\d+\s*E0*([0-9]+)\b",
    r"\bEP\s*0*([0-9]+)\b",
    r"\bE\s*0*([0-9]+)\b",
    r"(?:^|[\s\-_.])0*(\d{1,3})\s*[-_. ~～〜]\s*(?:4k|2160p|1080p|720p|480p|360p)\b",
    r"(?:-|—|–|~|～|〜)\s*0*([0-9]{1,4})\s*(?:[（(][^()（）]*[)）])?\s*$",
    r"^\s*0*([0-9]{1,4})(?=\s*[丨｜|])",
    r"^\s*0*([0-9]{1,4})\b",
    r"^\s*(\d+)\s*(?:[（(][^()（）]*[)）])?\s*$",
)

_EXPLICIT_EPISODE_PATTERNS = (
    r"第\s*([0-9零一二两三四五六七八九十百]+)\s*[集话期部回]",
    r"(?<!\d)0*([0-9]+)\s*[集话期]",
    r"\bS\d+\s*E0*([0-9]+)\b",
    r"\bEP\s*0*([0-9]+)\b",
    r"\bE\s*0*([0-9]+)\b",
    r"(?<!期)(?<!期\s)[上中下]\s*[集部篇卷](?![一-鿿])",
    r"(?<![一-鿿\w])[上中下](?![一-鿿\w])",
)

# 电影分部标记:"上/中/下"映射 1/2/3,可带 集/部/篇/卷 单位("上集/中部/下篇")
# 或括号形式("电影(上)"),也允许紧跟标题("流浪地球上集")。裸标记两侧必须是
# 非汉字/字母数字的边界,避免"史上/晚上/上线/碟中谍/中字"误报;带单位时单位后
# 不能再接汉字("网上集结/上部城市"排除)。"期上/期 上"是综艺分部(第2期上),
# 归 variety 专用逻辑(extract_variety_part)处理,这里让开。
_PART_MARKER_NUMBERS = {"上": 1, "中": 2, "下": 3}
_SUFFIXED_PART_MARKER_RE = re.compile(r"(?<!期)(?<!期\s)[上中下][集部篇卷](?![一-鿿])")
_BARE_PART_MARKER_RE = re.compile(r"(?<![一-鿿\w])(?<!期\s)[上中下](?![一-鿿\w])")


def extract_part_number(name: str) -> int | None:
    value = normalize_name(name)
    match = _SUFFIXED_PART_MARKER_RE.search(value) or _BARE_PART_MARKER_RE.search(value)
    if match is None:
        return None
    return _PART_MARKER_NUMBERS.get(match.group(0)[0])

# Matches an episode title that LEADS with its episode marker (no show-name
# prefix), e.g. "第十三集 <剧情>" / "13集 ..." / "EP13 ...". Platforms such as
# Tencent name episodes this way, so the show name is absent and name-similarity
# against the query cannot judge relevance — such candidates must be kept and
# matched by episode number instead. Variety episodes often lead with their
# air date, e.g. "2026-08-10 第2期上：<subtitle>", so a full date is also
# accepted as a leading marker. Part-split movies lead with "上集/中部/下" —
# same treatment.
_LEADING_EPISODE_MARKER = re.compile(
    r"^\s*(?:第\s*[0-9零一二两三四五六七八九十百]+\s*[集话期部回]"
    r"|0*[0-9]+\s*[集话期]"
    r"|S\d+\s*E\s*0*[0-9]+"
    r"|EP\s*0*[0-9]+"
    r"|E\s*0*[0-9]+"
    r"|[上中下][集部篇卷](?![一-鿿])"
    r"|[上中下](?![一-鿿\w])"
    r"|(?:19|20)\d{2}[\s._/-]?(?:0[1-9]|1[0-2])[\s._/-]?(?:0[1-9]|[12]\d|3[01])\b"
    r"|[0-9]+\s*$)",
    re.IGNORECASE,
)


def _starts_with_episode_marker(name: str) -> bool:
    return _LEADING_EPISODE_MARKER.match(name or "") is not None

_TECHNICAL_FILENAME_MARKERS = (
    "2160p",
    "1080p",
    "720p",
    "web-dl",
    "webrip",
    "bluray",
    "bdrip",
    "hdrip",
    "hdtv",
    "itunes",
    "amzn",
    "nf",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "hdr",
    "dv",
    "ddp",
    "aac",
    "atmos",
)

_QUALITY_ONLY_FILENAME_TOKENS = {
    "4k",
    "2160p",
    "1080p",
    "720p",
    "480p",
    "hdr",
    "dv",
    "hevc",
    "x264",
    "x265",
    "h264",
    "h265",
    "av1",
    "aac",
    "ddp",
    "atmos",
    "webdl",
    "webrip",
    "bluray",
    "bdrip",
    "itunes",
    "amzn",
    "nf",
    "国语",
    "粤语",
    "普通话",
    "原声",
    "超清",
    "高清",
    "蓝光",
    "正片",
    "完整版",
    "全片",
}

_VARIETY_HINT_TOKENS = (
    "纯享",
    "加更",
    "舞台",
    "未播",
    "会员版",
    "训练室",
    "reaction",
    "直拍",
)

_BONUS_TRACK_TITLE_TOKENS = (
    "片头片尾",
    "片头曲",
    "片尾曲",
)

_NUMERIC_X_SUFFIX_FILENAME_RE = re.compile(
    r"^\s*0*\d{1,4}\s*[xX]\.(?:mkv|mp4|avi|mov|m4v|ts|flv)\b",
    re.IGNORECASE,
)


def normalize_name(name: str) -> str:
    value = str(name).strip()
    for pattern in _NOISE_PATTERNS:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _cn_to_int(text: str) -> int | None:
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    current = 0
    units = {"十": 10, "百": 100}
    for char in text:
        if char in _CN_NUM:
            current = _CN_NUM[char]
            continue
        unit = units.get(char)
        if unit is None:
            return None
        total += (current or 1) * unit
        current = 0
    return total + current


def _looks_like_numeric_x_suffix_filename(value: str) -> bool:
    return _NUMERIC_X_SUFFIX_FILENAME_RE.search(str(value or "").strip()) is not None


def extract_episode_number(name: str) -> int | None:
    value = normalize_name(name)
    if _looks_like_numeric_x_suffix_filename(value):
        return None
    # 明确的集号比标题中的分部后缀更可靠。例如“第25集 神魔剑（下）”的“下”
    # 是剧情标题的一部分，不能把第25集误判为分部 3。
    for pattern in (*_EPISODE_PATTERNS[:3], *_EPISODE_PATTERNS[4:7]):
        match = re.search(pattern, value, re.IGNORECASE)
        if match is None:
            continue
        episode = _episode_number_from_match(value, match)
        if episode is not None:
            return episode
    # 分部标记("上集/中部/（下）")优先于结尾裸数字("X 上部 2"的 2
    # 更可能是分段文件序号),且请求侧/候选侧共用本函数,映射自洽。
    part = extract_part_number(value)
    if part is not None:
        return part
    for pattern in _EPISODE_PATTERNS:
        match = re.search(pattern, value, re.IGNORECASE)
        if match is None:
            continue
        episode = _episode_number_from_match(value, match)
        if episode is not None:
            return episode
    return None


def _episode_number_from_match(value: str, match: re.Match[str]) -> int | None:
    if _looks_like_episode_range_part(value, match):
        return None
    if _looks_like_complete_series_count_part(value, match):
        return None
    if _looks_like_date_fragment_part(value, match):
        return None
    raw = match.group(1)
    episode = int(raw) if raw.isdigit() else _cn_to_int(raw)
    return episode if episode is not None and 1 <= episode <= 10000 else None


def _looks_like_episode_range_part(value: str, match: re.Match[str]) -> bool:
    prefix = value[: match.start()]
    suffix = value[match.end() :]
    return bool(
        re.search(r"\d{1,4}\s*-\s*$", prefix)
        or re.match(r"^\s*-\s*\d{1,4}", suffix)
    )


def _looks_like_complete_series_count_part(value: str, match: re.Match[str]) -> bool:
    suffix = value[match.end() :]
    return re.match(r"^\s*全(?:$|[\s\-_.~～〜(（])", suffix) is not None


def _looks_like_date_fragment_part(value: str, match: re.Match[str]) -> bool:
    prefix = value[: match.start()]
    raw = match.group(1)
    if not raw.isdigit():
        return False
    fragment = int(raw)
    return bool(
        fragment <= 12
        and re.search(r"(?:19|20)\d{2}\s*$", prefix)
        or fragment <= 31
        and re.search(r"(?:19|20)\d{2}[-./]\d{1,2}\s*$", prefix)
    )


def has_explicit_episode_marker(name: str) -> bool:
    value = normalize_name(name)
    return any(re.search(pattern, value, re.IGNORECASE) is not None for pattern in _EXPLICIT_EPISODE_PATTERNS)


def _looks_like_technical_media_filename(name: str) -> bool:
    value = normalize_name(name).casefold()
    has_file_extension = re.search(r"\.(mkv|mp4|avi|mov|m4v|ts|flv)\b", value) is not None
    has_year_prefix = re.match(r"^\s*(?:19|20)\d{2}(?:[.\s_-]|$)", value) is not None
    has_marker = any(re.search(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", value) is not None for marker in _TECHNICAL_FILENAME_MARKERS)
    return has_marker and (has_file_extension or has_year_prefix)


def _looks_like_quality_only_media_filename(name: str) -> bool:
    value = normalize_name(name)
    if re.search(r"\.(mkv|mp4|avi|mov|m4v|ts|flv)\b", value, re.IGNORECASE) is None:
        return False
    basename = re.sub(r"\([^)]*\)\s*$", "", value).strip()
    basename = re.sub(r"\.(mkv|mp4|avi|mov|m4v|ts|flv)\b.*$", "", basename, flags=re.IGNORECASE).strip()
    if not basename:
        return False
    tokens = [
        token.casefold()
        for token in re.split(r"[.\s_\-]+", basename)
        if token.strip()
    ]
    if not tokens:
        return False
    return all(token in _QUALITY_ONLY_FILENAME_TOKENS for token in tokens)


def _path_basename(value: str) -> str:
    text = str(value or "").strip().rstrip("/\\")
    if not text:
        return ""
    return re.split(r"[\\/]", text)[-1]


_MEDIA_FILE_BASENAME_RE = re.compile(
    r"\.(?:mkv|mp4|avi|mov|m4v|ts|flv|mpg|mpeg|wmv|webm|rmvb)$",
    re.IGNORECASE,
)
_WEB_PAGE_BASENAME_RE = re.compile(r"\.(?:html?|php|asp|aspx|jsp)(?:[?#].*)?$", re.IGNORECASE)
# 至少两字母的 scheme（"site:"、"https:"），单字母的 "C:" 是 Windows 盘符，不算。
_SCHEME_LIKE_PATH_RE = re.compile(r"^[a-zA-Z]{2,}[a-zA-Z0-9+.\-]*:")


def _episode_path_basename(value: str) -> str:
    """取 path 的 basename 作为集数来源，先排除不可能携带集数的值。

    网盘占位条目用站点详情 ID 充当 path（如 ``site:duoduo:/index.php/vod/detail/id/9794.html``），
    其 basename 里的数字是页面编号而非集数；网页扩展名、scheme 形式的路径一律不作数，
    只有媒体文件名的 basename（含直链 URL 里的）才可信。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    basename = _path_basename(text)
    if not basename:
        return ""
    if _MEDIA_FILE_BASENAME_RE.search(basename):
        return basename
    if _WEB_PAGE_BASENAME_RE.search(basename) or _SCHEME_LIKE_PATH_RE.match(text):
        return ""
    return basename


def _episode_number_from_candidate(name: str) -> int | None:
    technical_filename = _looks_like_technical_media_filename(name)
    direct = extract_episode_number(name)
    if direct is not None and (not technical_filename or has_explicit_episode_marker(name)):
        return direct
    return None


def _play_item_episode_number(item: PlayItem) -> int | None:
    candidates: list[str] = []
    for value in (item.original_title, item.title, _episode_path_basename(item.path)):
        candidate = str(value or "").strip()
        if not candidate or candidate in candidates:
            continue
        candidates.append(candidate)
    for candidate in candidates:
        if (episode_number := _episode_number_from_candidate(candidate)) is not None:
            return episode_number
    return None


def _play_item_has_technical_filename(item: PlayItem) -> bool:
    seen: list[str] = []
    for value in (item.original_title, item.title, _path_basename(item.path)):
        candidate = str(value or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.append(candidate)
        if _looks_like_technical_media_filename(candidate):
            return True
    return False


def _looks_like_bonus_track_title(name: str) -> bool:
    value = normalize_name(name)
    if not value:
        return False
    return (
        any(token in value for token in _BONUS_TRACK_TITLE_TOKENS)
        or value.startswith("片头")
        or value.startswith("片尾")
    )


def _play_item_is_bonus_track(item: PlayItem) -> bool:
    seen: list[str] = []
    for value in (item.original_title, item.title, _path_basename(item.path)):
        candidate = str(value or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.append(candidate)
        if _looks_like_bonus_track_title(candidate):
            return True
    return False


def _resolved_playlist_index(current_item: PlayItem, playlist: Sequence[PlayItem] | None = None) -> int:
    explicit_index = current_item.index
    if playlist and 0 <= explicit_index < len(playlist) and playlist[explicit_index] is current_item:
        return explicit_index
    if playlist:
        for position, candidate in enumerate(playlist):
            if candidate is current_item:
                return position
    return explicit_index


def infer_playlist_episode_number(current_item: PlayItem, playlist: Sequence[PlayItem] | None = None) -> int | None:
    current_title = current_item.title or ""
    if _play_item_is_bonus_track(current_item):
        return None
    direct = _play_item_episode_number(current_item)
    if direct is not None:
        return direct
    if _play_item_has_technical_filename(current_item):
        return None
    if _looks_like_quality_only_media_filename(current_title) and (
        (playlist is None and current_item.index <= 0)
        or (playlist is not None and len(playlist) <= 1)
    ):
        return None
    current_index = _resolved_playlist_index(current_item, playlist)
    if not playlist:
        return current_index + 1 if current_index >= 0 else None
    if 0 <= current_index < len(playlist):
        indexed = _play_item_episode_number(playlist[current_index])
        if indexed is not None:
            return indexed
    aligned = [
        (position, episode)
        for position, item in enumerate(playlist)
        if (episode := _play_item_episode_number(item)) is not None
    ]
    if aligned:
        seq_like = sum(1 for index, episode in aligned if episode == index + 1)
        if seq_like >= max(1, len(aligned) // 2):
            return current_index + 1 if current_index >= 0 else None
    return current_index + 1 if current_index >= 0 else None


def _result_other_option_urls(result: DanmakuSourceSearchResult) -> list[str]:
    return [option.url for group in result.groups for option in group.options]


def is_other_fallback_only_result(result: DanmakuSourceSearchResult) -> bool:
    """搜索结果是否只含 other 兜底合成的候选。

    ``DanmakuService.search_danmu`` 在内置源与豆瓣发现全部落空、且 reg_src 是
    http 链接时，会把 reg_src 本身伪装成一个 "other" 候选。这类结果可以给当前
    条目兜底展示，但不能写入共享的源搜索缓存——标题级缓存对纯标题查询一律放行，
    必败候选会把后续新搜索挡到缓存过期为止。
    """
    urls = _result_other_option_urls(result)
    if not urls or not result.groups:
        return False
    return all(group.provider == "other" for group in result.groups)


def is_stale_other_fallback_result(result: DanmakuSourceSearchResult, reg_src: str) -> bool:
    """缓存里的 other-only 结果是否不属于当前 reg_src（历史污染条目）。"""
    if not is_other_fallback_only_result(result):
        return False
    normalized = str(reg_src or "").strip()
    return any(url.strip() != normalized for url in _result_other_option_urls(result))


def strip_episode_suffix(name: str) -> str:
    value = normalize_name(name)
    patterns = (
        r"\s+第\s*\d+\s*[集话期]\s*$",
        r"\s+\d+\s*[集话期]\s*$",
        r"\s+0*\d{1,4}\s*$",
        r"\s+S\d+\s*E\d+\s*$",
        r"\s+EP?\s*\d+\s*$",
        r"\s+E\s*\d+\s*$",
        r"\s*[（(]\s*[上中下]\s*[)）]\s*$",
        r"[上中下]\s*[集部篇卷]\s*$",
        r"\s+[上中下]\s*$",
    )
    # 组合查询可能叠多个后缀("标题 上集 1集"),循环剥到不动为止;每轮至少
    # 消费一个字符,必然终止。
    while True:
        for pattern in patterns:
            stripped = re.sub(pattern, "", value, flags=re.IGNORECASE)
            if stripped != value:
                value = stripped.strip()
                break
        else:
            return value


def _extract_variety_date_key(name: str) -> str | None:
    value = normalize_name(name)
    match = re.search(
        r"(?<!\d)((?:19|20)\d{2})[\s._/-]?(0[1-9]|1[0-2])[\s._/-]?(0[1-9]|[12]\d|3[01])(?:\s*期)?(?!\d)",
        value,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return "".join(match.groups())


def _extract_variety_issue_number(name: str) -> int | None:
    value = normalize_name(name)
    patterns = (
        r"第\s*([0-9零一二两三四五六七八九十百]+)\s*期",
        r"(?<!\d)0*([0-9]{1,4})\s*期",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if match is None:
            continue
        raw = match.group(1)
        issue = int(raw) if raw.isdigit() else _cn_to_int(raw)
        if issue is not None and 1 <= issue <= 10000:
            return issue
    return None


_VARIETY_PART_PATTERN = re.compile(
    r"期\s*(加更(?:[上下中终完])?|[上下中终完])(?![一-鿿])",
)


def extract_variety_part(name: str) -> str | None:
    """Variety sub-part token following 第N期, e.g. 上/中/下/加更上.

    Lets ``extract_variety_issue_key`` distinguish same-date/same-issue halves
    (第2期上 vs 第2期中 vs 第2期下) so danmaku matching picks the exact part.
    The negative CJK lookahead avoids false positives on words like 完整版/完结.
    Returns None when no part marker is present.
    """
    match = _VARIETY_PART_PATTERN.search(normalize_name(name))
    if match is None:
        return None
    return match.group(1)


def extract_variety_issue_key(name: str) -> str | None:
    date_key = _extract_variety_date_key(name)
    base: str | None = date_key
    if base is None:
        issue_number = _extract_variety_issue_number(name)
        if issue_number is not None:
            base = str(issue_number)
    if base is None:
        return None
    part = extract_variety_part(name)
    return f"{base}{part}" if part else base


def extract_variety_episode_label(title: str) -> str:
    """Build a danmaku search episode label for a variety filename/title.

    Reconstructs a compact ``[date] 第N期[part]`` suffix (e.g. ``20260810 第2期上``)
    from a filename like ``2026.08.10-第2期上.mp4`` so the composed danmaku query
    carries the variety signal. The date is included when present so both the
    query and provider candidates resolve to a date-based ``extract_variety_issue_key``
    and match. Returns "" when no date/issue/part can be recovered.
    """
    date_key = _extract_variety_date_key(title)
    issue = _extract_variety_issue_number(title)
    part = extract_variety_part(title)
    segments: list[str] = []
    if date_key is not None:
        segments.append(date_key)
    if issue is not None:
        segments.append(f"第{issue}期")
    label = " ".join(segments)
    if part:
        label = f"{label}{part}" if label else part
    return label


def is_likely_variety_title(name: str) -> bool:
    value = normalize_name(name)
    if re.search(r"第\s*[0-9零一二两三四五六七八九十百]+\s*期", value, re.IGNORECASE) is not None:
        return True
    if re.search(r"(?<!\d)0*[0-9]{1,4}\s*期", value, re.IGNORECASE) is not None:
        return True
    if _extract_variety_date_key(value) is not None:
        return True
    lowered = value.casefold()
    return any(token in lowered for token in _VARIETY_HINT_TOKENS)


_VARIETY_COLLECTION_MARKERS = ("综艺", "真人秀", "脱口秀", "variety")


def is_variety_collection(
    type_name: str = "",
    category_name: str = "",
    vod_tag: str = "",
    vod_content: str = "",
) -> bool:
    """Whether collection metadata marks this title as variety/reality/show.

    Complements filename-based :func:`is_likely_variety_title`: metadata genres
    (e.g. ``真人秀`` after hydration) are a more reliable variety signal when the
    per-episode filename is generic. Any marker in type/category/tag/content wins.
    """
    metadata_text = " ".join(
        str(value or "").strip().casefold()
        for value in (type_name, category_name, vod_tag, vod_content)
        if str(value or "").strip()
    )
    return any(marker in metadata_text for marker in _VARIETY_COLLECTION_MARKERS)


def has_variety_issue_marker(name: str) -> bool:
    """Strong variety marker in a title: 第N期/N期 or a variety hint token.

    Stricter than :func:`is_likely_variety_title`: a bare date does NOT qualify,
    because ordinary episode filenames often carry air dates (e.g.
    ``04-第4话…-2026-03-03``). Used to decide whether to rewrite an episode label
    as a variety issue label — date-only titles stay on the regular episode path.
    """
    value = normalize_name(name)
    if re.search(r"第\s*[0-9零一二两三四五六七八九十百]+\s*期", value, re.IGNORECASE) is not None:
        return True
    if re.search(r"(?<!\d)0*[0-9]{1,4}\s*期", value, re.IGNORECASE) is not None:
        return True
    lowered = value.casefold()
    return any(token in lowered for token in _VARIETY_HINT_TOKENS)


def strip_variety_issue_suffix(name: str) -> str:
    value = normalize_name(name)
    split_patterns = (
        r"^(?P<title>.+?)\s+(?:(?:19|20)\d{2}[\s._/-]?(?:0[1-9]|1[0-2])[\s._/-]?(?:0[1-9]|[12]\d|3[01])(?:\s*期)?)(?:\s+.*)?$",
        r"^(?P<title>.*?\D)\s*第\s*[0-9零一二两三四五六七八九十百]+\s*期(?:[上下中终完]?)?(?:[：: ].*)?$",
        r"^(?P<title>.*?\D)\s*0*[0-9]{1,4}\s*期(?:[上下中终完]?)?(?:[：: ].*)?$",
    )
    for pattern in split_patterns:
        match = re.match(pattern, value, re.IGNORECASE)
        if match is None:
            continue
        title = match.group("title").strip()
        if title:
            return title
    patterns = (
        r"[\s._/-]+第\s*[0-9零一二两三四五六七八九十百]+\s*期\s*$",
        r"[\s._/-]+0*[0-9]{1,4}\s*期\s*$",
        r"[\s._/-]+(?:19|20)\d{2}[\s._/-]?(?:0[1-9]|1[0-2])[\s._/-]?(?:0[1-9]|[12]\d|3[01])(?:\s*期)?\s*$",
    )
    for pattern in patterns:
        stripped = re.sub(pattern, "", value, flags=re.IGNORECASE)
        if stripped != value:
            return stripped.strip()
    return value


def match_provider(reg_src: str) -> str | None:
    host = (urlparse(reg_src).hostname or reg_src or "").lower()
    if "qq.com" in host:
        return "tencent"
    if "youku.com" in host:
        return "youku"
    if "bilibili.com" in host or "b23.tv" in host:
        return "bilibili"
    if "iqiyi.com" in host:
        return "iqiyi"
    if "mgtv.com" in host:
        return "mgtv"
    if "sohu.com" in host:
        return "sohu"
    if "miguvideo.com" in host:
        return "migu"
    return None


def extract_official_link_url(detail_fields: object) -> str:
    """First ``官方链接`` action URL that maps to a known danmaku provider.

    Metadata hydration records official platform links as a ``官方链接``
    ``PlaybackDetailField`` whose value parts carry ``link`` actions with concrete
    URLs (e.g. ``https://v.qq.com/x/cover/…``). Returns the first such URL whose
    host :func:`match_provider` recognizes, else ``""``. Duck-typed over the
    detail-field objects to avoid coupling this helper to the models dataclasses.
    """
    for field in getattr(detail_fields, "__iter__", lambda: [])() or []:
        if str(getattr(field, "label", "") or "").strip() != "官方链接":
            continue
        for part in getattr(field, "value_parts", []) or []:
            action = getattr(part, "action", None)
            if action is None or str(getattr(action, "type", "") or "") != "link":
                continue
            url = str(getattr(action, "value", "") or "").strip()
            if url and match_provider(url) is not None:
                return url
    return ""


def extract_cover_id(url: str) -> str:
    """Tencent (v.qq.com) cover id from an episode/cover URL, else empty.

    Used to disambiguate same-named shows: the user's playing URL (reg_src) carries
    the authoritative cover id, so a candidate whose cover id matches is the user's
    show, not a different same-named title.
    """
    match = re.search(r"/x/cover(?:_seo)?/([^/]+)/", url or "")
    return match.group(1) if match is not None else ""


def _simplify_name(name: str) -> str:
    value = normalize_name(name).casefold()
    value = re.sub(
        r"第\s*([0-9零一二两三四五六七八九十百]+)\s*季",
        lambda match: f"第{_cn_to_int(match.group(1)) if not match.group(1).isdigit() else int(match.group(1))}季",
        value,
    )
    value = re.sub(r"第\s*\d+\s*[集话期]", "", value)
    value = re.sub(r"(?<!\d)\d+\s*[集话期]", "", value)
    value = re.sub(r"\s+0*\d{1,4}\s*$", "", value)
    value = re.sub(r"\bs\d+\s*e\d+\b", "", value)
    value = re.sub(r"\bep?\s*\d+\b", "", value)
    value = re.sub(r"\be\s*\d+\b", "", value)
    value = re.sub(r"[\W_]+", "", value)
    return value


_EPISODE_MARKER_SPLIT_RE = re.compile("|".join(_EXPLICIT_EPISODE_PATTERNS), re.IGNORECASE)


def _extract_title_sequel_number(name: str) -> int | None:
    # Sequel markers ("第2季", "Show 2") live in the show-name part before the
    # episode marker. Numbers after the marker are episode-subtitle numbering
    # (e.g. "第138话 外海风云14") and must not be read as sequel numbers.
    value = normalize_name(strip_episode_suffix(name))
    marker = _EPISODE_MARKER_SPLIT_RE.search(value)
    if marker is not None:
        value = value[: marker.start()]
    value = _simplify_name(value)
    matches = re.findall(r"(?<=[^\W\d_])(\d{1,2})(?=[^\W\d_]|$)", value, re.UNICODE)
    if not matches:
        return None
    sequel = int(matches[-1])
    return sequel if 1 <= sequel <= 20 else None


def _has_sequel_number_mismatch(left: str, right: str) -> bool:
    left_seq = _extract_title_sequel_number(left)
    right_seq = _extract_title_sequel_number(right)
    if left_seq is None and right_seq is None:
        return False
    return left_seq != right_seq


def similarity_score(left: str, right: str) -> float:
    return SequenceMatcher(None, _simplify_name(left), _simplify_name(right)).ratio()


def episode_title_matches(target: str, candidate: str) -> bool:
    if _has_sequel_number_mismatch(target, candidate):
        return False
    target_base = _simplify_name(strip_episode_suffix(target))
    candidate_base = _simplify_name(strip_episode_suffix(candidate))
    if not target_base or not candidate_base:
        return True
    return candidate_base == target_base or candidate_base.startswith(target_base)


def episode_matches_request(candidate: str, requested_episode: int, target: str) -> bool:
    """Whether ``candidate`` is episode ``requested_episode`` of the show in ``target``.

    Platforms such as Tencent name episodes by plot subtitle without repeating the
    show name (e.g. "第十三集 <剧情>"); for such marker-led titles an episode-number
    match with no sequel mismatch is enough. For titles that carry a show-name
    prefix, fall back to :func:`episode_title_matches` so a different show sharing
    the same episode number is still rejected.
    """
    if extract_episode_number(candidate) != requested_episode:
        return False
    if _has_sequel_number_mismatch(target, candidate):
        return False
    if _starts_with_episode_marker(candidate):
        return True
    return episode_title_matches(target, candidate)


def should_filter_name(target: str, candidate: str) -> bool:
    if _starts_with_episode_marker(candidate):
        return False
    if _has_sequel_number_mismatch(target, candidate):
        return True
    left = _simplify_name(target)
    right = _simplify_name(candidate)
    if not left or not right:
        return False
    if left in right or right in left:
        return False
    return similarity_score(left, right) < 0.55


_XML_ILLEGAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_xml_text(value: str) -> str:
    # Strip XML-1.0-illegal control characters (keep \t \n \r). A single such char
    # in any danmaku would make the whole payload unparseable -> empty render.
    return _XML_ILLEGAL_CONTROL_RE.sub("", value)


def build_xml(records: Sequence[DanmakuRecord]) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?><i>']
    for record in records:
        content = _sanitize_xml_text(record.content)
        parts.append(
            f'<d p="{record.time_offset},{record.pos},25,{record.color}">{escape(content, quote=False)}</d>'
        )
    parts.append("</i>")
    return "".join(parts)
