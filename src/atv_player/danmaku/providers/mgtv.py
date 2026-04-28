from __future__ import annotations

import re
from math import ceil

import httpx

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem
from atv_player.danmaku.utils import should_filter_name

_NOISE_TITLE_PATTERN = re.compile(r"(预告|花絮|彩蛋|幕后|reaction|精彩片段|看点|特辑)", re.IGNORECASE)
_RGB_PATTERN = re.compile(r"rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)", re.IGNORECASE)


class MgtvDanmakuProvider:
    key = "mgtv"

    def __init__(self, get=httpx.get) -> None:
        self._get = get

    def search(self, name: str, original_name: str | None = None) -> list[DanmakuSearchItem]:
        response = self._get(
            "https://mobileso.bz.mgtv.com/msite/search/v2",
            params={
                "q": name,
                "pc": 30,
                "pn": 1,
                "sort": -99,
                "ty": 0,
                "du": 0,
                "pt": 0,
                "corr": 1,
                "abroad": 0,
                "_support": 10000000000000000,
            },
            headers=self._json_headers(),
            follow_redirects=True,
            timeout=10.0,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DanmakuSearchError(f"MGTV danmaku search failed: HTTP {response.status_code}") from exc
        contents = (payload.get("data") or {}).get("contents")
        if not isinstance(contents, list):
            raise DanmakuSearchError("MGTV danmaku search failed: invalid payload")

        results: list[DanmakuSearchItem] = []
        for content in contents:
            if str(content.get("type") or "") != "media":
                continue
            for item in content.get("data") or []:
                if str(item.get("source") or "") != "imgo":
                    continue
                raw_url = str(item.get("url") or "")
                match = re.search(r"/b/(\d+)", raw_url)
                if match is None:
                    continue
                title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
                if not title or should_filter_name(name, title):
                    continue
                for episode_name, episode_url in self._expand_candidate(title, match.group(1)):
                    results.append(DanmakuSearchItem(provider=self.key, name=episode_name, url=episode_url))
        return results

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        collection_id, video_id = self._parse_play_url(page_url)
        duration_seconds = self._video_duration(collection_id, video_id)
        segment_urls = self._segment_urls(collection_id, video_id, duration_seconds)
        records: list[DanmakuRecord] = []
        for segment_url in segment_urls:
            response = self._get(
                segment_url,
                headers=self._json_headers(),
                follow_redirects=True,
                timeout=10.0,
            )
            records.extend(self._segment_records(response.json()))
        return records

    def supports(self, page_url: str) -> bool:
        return "mgtv.com" in page_url

    def _expand_candidate(self, title: str, collection_id: str) -> list[tuple[str, str]]:
        items = self._collection_items(collection_id)
        if not items:
            return []

        expanded: list[tuple[str, str]] = []
        for item in items:
            if str(item.get("src_clip_id") or collection_id) != collection_id:
                continue
            if str(item.get("isnew") or "") == "2":
                continue
            episode_title = self._episode_title(item)
            if not episode_title or self._is_noise_title(episode_title):
                continue
            video_id = str(item.get("video_id") or "").strip()
            if not video_id:
                continue
            expanded.append((f"{title} {episode_title}".strip(), self._episode_url(collection_id, video_id)))
        if expanded:
            return expanded

        best = self._pick_movie_item(items)
        if best is None:
            return []
        video_id = str(best.get("video_id") or "").strip()
        if not video_id:
            return []
        movie_title = self._candidate_name(title, best)
        return [(movie_title, self._episode_url(collection_id, video_id))]

    def _json_headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Referer": "https://www.mgtv.com/",
        }

    def _collection_items(self, collection_id: str) -> list[dict]:
        months = [""]
        output: list[dict] = []
        index = 0
        while index < len(months):
            month = months[index]
            response = self._get(
                "https://pcweb.api.mgtv.com/variety/showlist",
                params={
                    "allowedRC": 1,
                    "collection_id": collection_id,
                    "month": month,
                    "page": 1,
                    "_support": 10000000,
                },
                headers=self._json_headers(),
                follow_redirects=True,
                timeout=10.0,
            )
            payload = response.json()
            data = payload.get("data") or {}
            if index == 0:
                for tab in (data.get("tab_m") or [])[1:]:
                    next_month = str(tab.get("m") or "").strip()
                    if next_month and next_month not in months:
                        months.append(next_month)
            output.extend(data.get("list") or [])
            index += 1
        return output

    def _pick_movie_item(self, items: list[dict]) -> dict | None:
        for item in items:
            if str(item.get("isIntact") or "") == "1":
                return item
        for item in items:
            if str(item.get("isnew") or "") != "2":
                return item
        return items[0] if items else None

    def _episode_title(self, item: dict) -> str:
        return " ".join(part for part in [str(item.get("t2") or "").strip(), str(item.get("t1") or "").strip()] if part).strip() or str(
            item.get("t3") or ""
        ).strip()

    def _candidate_name(self, title: str, item: dict) -> str:
        suffix = self._episode_title(item)
        return f"{title} {suffix}".strip() if suffix else title

    def _episode_url(self, collection_id: str, video_id: str) -> str:
        return f"https://www.mgtv.com/b/{collection_id}/{video_id}.html"

    def _is_noise_title(self, title: str) -> bool:
        return bool(_NOISE_TITLE_PATTERN.search(title))

    def _parse_play_url(self, page_url: str) -> tuple[str, str]:
        match = re.search(r"/b/(\d+)/([^/?#]+)\.html", page_url)
        if match is None:
            raise DanmakuResolveError("MGTV danmaku resolve failed: invalid play url")
        return match.group(1), match.group(2)

    def _video_duration(self, collection_id: str, video_id: str) -> int:
        response = self._get(
            "https://pcweb.api.mgtv.com/video/info",
            params={"cid": collection_id, "vid": video_id},
            headers=self._json_headers(),
            follow_redirects=True,
            timeout=10.0,
        )
        payload = response.json()
        time_text = str((((payload.get("data") or {}).get("info") or {}).get("time") or "")).strip()
        seconds = self._time_to_seconds(time_text)
        if seconds <= 0:
            raise DanmakuResolveError("MGTV danmaku resolve failed: missing duration")
        return seconds

    def _segment_urls(self, collection_id: str, video_id: str, duration_seconds: int) -> list[str]:
        response = self._get(
            "https://galaxy.bz.mgtv.com/getctlbarrage",
            params={
                "version": "8.1.39",
                "abroad": 0,
                "uuid": "",
                "os": "10.15.7",
                "platform": 0,
                "mac": "",
                "vid": video_id,
                "pid": "",
                "cid": collection_id,
                "ticket": "",
            },
            headers=self._json_headers(),
            follow_redirects=True,
            timeout=10.0,
        )
        payload = response.json()
        data = payload.get("data") or {}
        cdn_list = str(data.get("cdn_list") or "").strip()
        cdn_version = str(data.get("cdn_version") or "").strip()
        total_segments = max(1, ceil(duration_seconds / 60))
        if cdn_list and cdn_version:
            first_cdn = cdn_list.split(",")[0].strip()
            return [f"https://{first_cdn}/{cdn_version}/{index}.json" for index in range(total_segments)]
        return [
            f"https://galaxy.bz.mgtv.com/rdbarrage?vid={video_id}&cid={collection_id}&time={index * 60000}"
            for index in range(total_segments)
        ]

    def _segment_records(self, payload: dict) -> list[DanmakuRecord]:
        items = ((payload.get("data") or {}).get("items") or [])
        records: list[DanmakuRecord] = []
        for item in items:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            records.append(
                DanmakuRecord(
                    time_offset=round(float(item.get("time") or 0) / 1000, 3),
                    pos=self._comment_pos(item),
                    color=str(self._comment_color(item)),
                    content=content,
                )
            )
        return records

    def _comment_pos(self, item: dict) -> int:
        position = item.get("v2_position")
        if position == 1:
            return 5
        if position == 2:
            return 4
        return 1

    def _comment_color(self, item: dict) -> int:
        color = item.get("v2_color") or {}
        left = self._rgb_to_int(color.get("color_left"))
        right = self._rgb_to_int(color.get("color_right"))
        if left < 0 and right < 0:
            return 16777215
        if left < 0:
            return right
        if right < 0:
            return left
        return (left + right) // 2

    def _rgb_to_int(self, value: object) -> int:
        if not isinstance(value, str):
            return -1
        match = _RGB_PATTERN.fullmatch(value.strip())
        if match is None:
            return -1
        red, green, blue = (int(part) for part in match.groups())
        if any(part < 0 or part > 255 for part in (red, green, blue)):
            return -1
        return (red << 16) + (green << 8) + blue

    def _time_to_seconds(self, value: str) -> int:
        parts = [int(part) for part in value.split(":") if part.isdigit()]
        if len(parts) == 2:
            minutes, seconds = parts
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return hours * 3600 + minutes * 60 + seconds
        return 0
