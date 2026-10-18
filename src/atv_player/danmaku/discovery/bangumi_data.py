from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from opencc import OpenCC

from atv_player.danmaku.utils import (
    ROMAN_NUMERALS,
    strip_episode_suffix,
    strip_variety_issue_suffix,
)
from atv_player.paths import app_cache_dir

logger = logging.getLogger(__name__)

# bangumi-data dataset (https://github.com/bangumi-data/bangumi-data). Custom
# fork @wan0ge/bangumi-data is preferred — it carries the bilibili season_id /
# gamer video_sn fields that the official package lacks. CDN fallback chain.
_CDN_SOURCES = (
    "https://cdn.jsdelivr.net/npm/@wan0ge/bangumi-data@0.3/dist/data.json",
    "https://unpkg.com/@wan0ge/bangumi-data@0.3/dist/data.json",
    "https://cdn.jsdelivr.net/npm/bangumi-data@0.3/dist/data.json",
    "https://unpkg.com/bangumi-data@0.3/dist/data.json",
)
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
    ),
}

_BILIBILI_SITES = frozenset(
    {"bilibili", "bilibili_hk_mo_tw", "bilibili_hk_mo", "bilibili_tw"}
)
_GAMER_SITES = frozenset({"gamer", "gamer_hk"})

_CACHE_VERSION = "v1"
_CACHE_FILENAME = f"bangumi-data-{_CACHE_VERSION}.json"
_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_DOWNLOAD_TIMEOUT = 20.0

_SIMPLIFIER = OpenCC("t2s")

# Explicit season markers only — never read a bare trailing number as a season,
# so titles like "高达00" are not misread as season 0/79.
_SEASON_PATTERNS = (
    re.compile(r"第\s*([0-9]+|[一二三四五六七八九十]+)\s*[季期部]"),
    re.compile(r"(?:^|[\s第])(?:season|s)\s*0*([0-9]{1,2})\b", re.IGNORECASE),
)
# 尾部罗马数字季号("无职转生Ⅱ"),ASCII 形式无法与型号区分不参与(对齐 danmu_api)
_ROMAN_SEASON_PATTERN = re.compile(r"第?\s*([Ⅰ-Ⅻⅰ-ⅻ])\s*[季部]?\s*$")
_CN_NUMERAL = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(frozen=True, slots=True)
class BangumiHit:
    provider: str
    page_url: str
    title: str


def _simplify(value: str) -> str:
    return _SIMPLIFIER.convert(value)


def _cn_to_int(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if not text:
        return None
    if text == "十":
        return 10
    if "十" in text:
        parts = text.split("十")
        tens = _CN_NUMERAL.get(parts[0]) if parts[0] else 1
        ones = _CN_NUMERAL.get(parts[1]) if len(parts) > 1 and parts[1] else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    return _CN_NUMERAL.get(text)


def _extract_season(text: str) -> int | None:
    for pattern in _SEASON_PATTERNS:
        match = pattern.search(text or "")
        if match:
            season = _cn_to_int(match.group(1))
            if season and 1 <= season <= 99:
                return season
    match = _ROMAN_SEASON_PATTERN.search(text or "")
    if match:
        return ROMAN_NUMERALS.get(match.group(1))
    return None


def _prune(raw: object) -> list[dict]:
    """Keep only items that carry a usable bilibili season id or bahamut video_sn."""
    if not isinstance(raw, dict):
        return []
    raw_items = raw.get("items")
    if not isinstance(raw_items, list):
        return []
    pruned: list[dict] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        bilibili_season: str | None = None
        bahamut_sn: str | None = None
        sites = raw_item.get("sites")
        if isinstance(sites, list):
            for site in sites:
                if not isinstance(site, dict):
                    continue
                site_key = str(site.get("site") or "")
                if site_key in _BILIBILI_SITES and bilibili_season is None:
                    season_id = str(site.get("season_id") or "").strip()
                    site_id = str(site.get("id") or "").strip()
                    candidate = season_id or (site_id if site_id.isdigit() else "")
                    if candidate:
                        bilibili_season = candidate
                elif site_key in _GAMER_SITES and bahamut_sn is None:
                    video_sn = str(site.get("video_sn") or "").strip()
                    if video_sn:
                        bahamut_sn = video_sn
        if not bilibili_season and not bahamut_sn:
            continue
        titles = [str(raw_item.get("title") or "").strip()]
        translates = raw_item.get("titleTranslate")
        if isinstance(translates, dict):
            for values in translates.values():
                if isinstance(values, list):
                    titles.extend(str(value or "").strip() for value in values if value)
        titles = [title for title in titles if title]
        if not titles:
            continue
        pruned.append(
            {
                "title": titles[0],
                "type": str(raw_item.get("type") or ""),
                "titles": titles,
                "bilibili_season": bilibili_season,
                "bahamut_video_sn": bahamut_sn,
            }
        )
    return pruned


class BangumiDataDiscovery:
    """Resolve anime title + season to authoritative provider IDs.

    Given a (possibly season-suffixed) title, returns bilibili `ss{season_id}`
    and bahamut `video_sn` hits that the service routes to the matching
    provider's ``expand_page_url``. The dataset is anime-only, so non-anime
    titles naturally yield no hits.
    """

    def __init__(
        self,
        get: Callable[..., httpx.Response] = httpx.get,
        post: Callable[..., httpx.Response] | None = None,
    ) -> None:
        self._get = get
        self._items: list[dict] | None = None

    def search(self, keyword: str) -> list[BangumiHit]:
        if not self.ensure_loaded() or self._items is None:
            return []
        season = _extract_season(keyword)
        base = (
            strip_variety_issue_suffix(strip_episode_suffix(keyword)).strip()
            or keyword.strip()
        )
        if not base:
            return []
        queries = {q for q in (base, _simplify(base)) if q}
        queries = {q.lower() for q in queries}

        matched: list[dict] = []
        for item in self._items:
            variants = {_simplify(title).lower() for title in item["titles"]}
            if any(self._query_hits(query, variants) for query in queries):
                matched.append(item)

        if season is not None and matched:
            filtered = [item for item in matched if self._season_ok(item, season)]
            if filtered:
                matched = filtered

        hits: list[BangumiHit] = []
        for item in matched:
            bilibili_season = item["bilibili_season"]
            if bilibili_season:
                hits.append(
                    BangumiHit(
                        provider="bilibili",
                        page_url=f"https://www.bilibili.com/bangumi/play/ss{bilibili_season}",
                        title=item["title"],
                    )
                )
            bahamut_sn = item["bahamut_video_sn"]
            if bahamut_sn:
                hits.append(
                    BangumiHit(
                        provider="bahamut",
                        page_url=f"bahamut://series/{bahamut_sn}",
                        title=item["title"],
                    )
                )
        return hits

    @staticmethod
    def _query_hits(query: str, variants: set[str]) -> bool:
        if not query:
            return False
        if query in variants:
            return True
        if len(query) < 2:
            return False
        return any(
            query in variant or variant in query for variant in variants if variant
        )

    @staticmethod
    def _season_ok(item: dict, requested_season: int) -> bool:
        item_season: int | None = None
        for title in item["titles"]:
            extracted = _extract_season(title)
            if extracted is not None:
                item_season = extracted
                break
        if requested_season > 1:
            return (item_season or 1) == requested_season
        return item_season is None or item_season == 1

    def ensure_loaded(self) -> bool:
        if self._items is not None:
            return True
        loaded = self._load_from_disk()
        if loaded:
            return True
        raw = self._download()
        if raw is None:
            return False
        self._items = _prune(raw)
        self._save_to_disk({"items": self._items})
        return True

    def _cache_path(self):
        cache_dir = app_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / _CACHE_FILENAME

    def _load_from_disk(self) -> bool:
        try:
            path = self._cache_path()
            if not path.exists():
                return False
            if time.time() - path.stat().st_mtime > _CACHE_MAX_AGE_SECONDS:
                return False
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Bangumi-data cache load failed")
            return False
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list) or not items:
            return False
        self._items = items
        return True

    def _save_to_disk(self, payload: dict) -> None:
        try:
            self._cache_path().write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            logger.exception("Bangumi-data cache save failed")

    def _download(self) -> object | None:
        for url in _CDN_SOURCES:
            try:
                response = self._get(
                    url,
                    headers=_HEADERS,
                    timeout=_DOWNLOAD_TIMEOUT,
                    follow_redirects=True,
                )
                if getattr(response, "status_code", 200) != 200:
                    continue
                data = response.json()
            except Exception:
                logger.warning(
                    "Bangumi-data download failed url=%s", url, exc_info=True
                )
                continue
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                logger.info(
                    "Bangumi-data loaded from %s (%d raw items)",
                    url,
                    len(data["items"]),
                )
                return data
        logger.warning("Bangumi-data download failed: all CDN sources exhausted")
        return None
