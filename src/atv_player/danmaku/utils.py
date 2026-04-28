from __future__ import annotations

import re
from difflib import SequenceMatcher
from html import escape
from typing import Sequence
from urllib.parse import urlparse

from atv_player.danmaku.models import DanmakuRecord

_NOISE_PATTERNS = (
    r"【[^】]*】",
    r"\[[^\]]*\]",
    r"\([^)]*(高清|超清|蓝光|qq\.com|youku\.com)[^)]*\)",
)

_EPISODE_PATTERNS = (
    r"第\s*(\d+)\s*[集话期]",
    r"\s+(\d+)\s*[集话期]",
    r"(?<!\d)(\d+)\s*[集话期]",
    r"\bS\d+\s*E(\d+)\b",
    r"\bEP?\s*(\d+)\b",
    r"\bE\s*(\d+)\b",
    r"^\s*(\d+)\s*(?:[（(][^()（）]*[)）])?\s*$",
)


def normalize_name(name: str) -> str:
    value = str(name).strip()
    for pattern in _NOISE_PATTERNS:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def extract_episode_number(name: str) -> int | None:
    value = normalize_name(name)
    for pattern in _EPISODE_PATTERNS:
        match = re.search(pattern, value, re.IGNORECASE)
        if match is None:
            continue
        return int(match.group(1))
    return None


def strip_episode_suffix(name: str) -> str:
    value = normalize_name(name)
    patterns = (
        r"\s+第\s*\d+\s*[集话期]\s*$",
        r"\s+\d+\s*[集话期]\s*$",
        r"\s+S\d+\s*E\d+\s*$",
        r"\s+EP?\s*\d+\s*$",
        r"\s+E\s*\d+\s*$",
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
    return None


def _simplify_name(name: str) -> str:
    value = normalize_name(name).casefold()
    value = re.sub(r"第\s*\d+\s*[集话期]", "", value)
    value = re.sub(r"(?<!\d)\d+\s*[集话期]", "", value)
    value = re.sub(r"\bs\d+\s*e\d+\b", "", value)
    value = re.sub(r"\bep?\s*\d+\b", "", value)
    value = re.sub(r"\be\s*\d+\b", "", value)
    value = re.sub(r"[\W_]+", "", value)
    return value


def similarity_score(left: str, right: str) -> float:
    return SequenceMatcher(None, _simplify_name(left), _simplify_name(right)).ratio()


def should_filter_name(target: str, candidate: str) -> bool:
    left = _simplify_name(target)
    right = _simplify_name(candidate)
    if not left or not right:
        return False
    if left in right or right in left:
        return False
    return similarity_score(left, right) < 0.55


def build_xml(records: Sequence[DanmakuRecord]) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?><i>']
    for record in records:
        parts.append(
            f'<d p="{record.time_offset},{record.pos},25,{record.color}">{escape(record.content, quote=False)}</d>'
        )
    parts.append("</i>")
    return "".join(parts)
