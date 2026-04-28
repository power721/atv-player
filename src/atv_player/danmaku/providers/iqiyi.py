from __future__ import annotations

import html
import json
import math
import re
import xml.etree.ElementTree as ET
import zlib

import httpx

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem
from atv_player.danmaku.utils import normalize_name, similarity_score


class IqiyiDanmakuProvider:
    key = "iqiyi"
    _SEARCH_URL = "https://search.video.iqiyi.com/o"
    _SEARCH_HEADERS = {"user-agent": "Mozilla/5.0", "referer": "https://www.iqiyi.com/"}
    _PAGE_HEADERS = {"user-agent": "Mozilla/5.0", "referer": "https://www.iqiyi.com/"}
    _DROP_CHANNEL_KEYWORDS = ("生活", "教育")
    _DROP_TITLE_KEYWORDS = ("精彩看点", "精彩片段", "精彩分享")

    def __init__(self, get=httpx.get) -> None:
        self._get = get
        self._metadata_by_url: dict[str, dict[str, str | int | None]] = {}

    def supports(self, page_url: str) -> bool:
        return "iqiyi.com" in page_url

    def search(self, name: str) -> list[DanmakuSearchItem]:
        response = self._get(
            self._SEARCH_URL,
            params={
                "if": "html5",
                "key": normalize_name(name),
                "pageNum": 1,
                "pageSize": 20,
                "video_allow_3rd": 0,
            },
            headers=dict(self._SEARCH_HEADERS),
            follow_redirects=True,
            timeout=10.0,
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise DanmakuSearchError("爱奇艺弹幕搜索结果解析失败") from exc
        return self._extract_search_items(payload, name)

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        page_url = self._normalize_iqiyi_url(page_url)
        response = self._get(
            page_url,
            headers=dict(self._PAGE_HEADERS),
            follow_redirects=True,
            timeout=10.0,
        )
        page_info = self._try_extract_page_info(response.text)
        cached_metadata = self._metadata_by_url.get(page_url) or self._metadata_by_url.get(self._swap_scheme(page_url))
        if not page_info and cached_metadata is None:
            raise DanmakuResolveError("爱奇艺页面缺少 playPageInfo")
        danmaku_fields = self._resolve_danmaku_fields(page_info, page_url)
        tvid = danmaku_fields["tv_id"]
        album_id = danmaku_fields["album_id"]
        category_id = danmaku_fields["category_id"]
        if not tvid or album_id in ("", None) or category_id in ("", None):
            raise DanmakuResolveError("爱奇艺页面缺少弹幕所需字段")
        duration_seconds = self._resolve_duration_seconds(page_info, cached_metadata)
        total_pages = max(1, math.ceil(duration_seconds / 300.0))
        records: list[DanmakuRecord] = []
        seen: set[tuple[float, str]] = set()
        parse_failures = 0
        for page_index in range(1, total_pages + 1):
            segment_url = self._segment_url(tvid, page_index)
            segment_response = self._get(
                segment_url,
                params={
                    "rn": "0.0123456789123456",
                    "business": "danmu",
                    "is_iqiyi": "true",
                    "is_video_page": "true",
                    "tvid": tvid,
                    "albumid": album_id,
                    "categoryid": category_id,
                    "qypid": "01010021010000000000",
                },
                headers=dict(self._PAGE_HEADERS),
                follow_redirects=True,
                timeout=10.0,
            )
            try:
                xml_text = zlib.decompress(segment_response.content, 15 + 32).decode("utf-8", errors="ignore")
                for record in self._parse_segment_records(xml_text, duration_seconds):
                    key = (record.time_offset, record.content)
                    if key in seen:
                        continue
                    seen.add(key)
                    records.append(record)
            except Exception:
                parse_failures += 1
        if parse_failures == total_pages and not records:
            raise DanmakuResolveError("爱奇艺弹幕分片解析失败")
        records.sort(key=lambda record: (record.time_offset, record.content))
        return records

    def _extract_search_items(self, payload: dict, query_name: str) -> list[DanmakuSearchItem]:
        data = payload.get("data")
        if isinstance(data, dict) and "search result is empty" in data:
            return []
        if not isinstance(data, dict) or not isinstance(data.get("docinfos"), list):
            raise DanmakuSearchError("爱奇艺弹幕搜索结果解析失败")
        results: list[DanmakuSearchItem] = []
        for item in data["docinfos"]:
            if self._should_drop_search_item(item):
                continue
            album_info = item.get("albumDocInfo") or {}
            videos = self._collect_search_videos(item, album_info)
            for video in videos:
                title = str(video.get("itemTitle") or "").strip()
                url = self._normalize_iqiyi_url(str(video.get("itemLink") or "").strip())
                if not title or not url:
                    continue
                ratio = similarity_score(query_name, title)
                self._remember_metadata(url, self._video_metadata(video, album_info))
                results.append(DanmakuSearchItem(provider=self.key, name=title, url=url, ratio=ratio, simi=ratio))
        return results

    def _collect_search_videos(self, item: dict, album_info: dict) -> list[dict]:
        videos = list(item.get("videoinfos") or album_info.get("videoinfos") or [])
        if not self._should_expand_album_videos(videos, album_info):
            return videos
        try:
            expanded = self._expand_album_videos(album_info)
        except httpx.HTTPError:
            return videos
        if expanded:
            return expanded
        return videos

    def _should_expand_album_videos(self, videos: list[dict], album_info: dict) -> bool:
        item_total = self._to_int(album_info.get("itemTotalNumber"))
        album_link = str(album_info.get("albumLink") or "").strip()
        if item_total is None or item_total <= 0 or not album_link:
            return False
        if len(videos) < item_total:
            return True
        numbers = sorted(
            episode
            for video in videos
            if (episode := self._to_int(video.get("itemNumber") or video.get("order"))) is not None
        )
        if len(numbers) < item_total:
            return True
        return numbers != list(range(1, item_total + 1))

    def _expand_album_videos(self, album_info: dict) -> list[dict]:
        album_link = self._normalize_iqiyi_url(str(album_info.get("albumLink") or "").strip())
        if not album_link:
            return []
        url = album_link if "?" in album_link else f"{album_link}?jump=0"
        response = self._get(
            url,
            headers=dict(self._PAGE_HEADERS),
            follow_redirects=True,
            timeout=10.0,
        )
        match = re.search(r'id="album-avlist-data"\s+value=\'([^\']+)\'', response.text, re.S)
        if match is None:
            return []
        try:
            payload = json.loads(html.unescape(match.group(1)))
        except json.JSONDecodeError:
            return []
        episodes = payload.get("epsodelist") or []
        if not episodes and payload.get("urlParam"):
            api_url = self._album_avlist_api_url(str(payload.get("urlParam") or ""))
            if api_url:
                api_response = self._get(
                    api_url,
                    headers=dict(self._PAGE_HEADERS),
                    follow_redirects=True,
                    timeout=10.0,
                )
                try:
                    api_payload = api_response.json()
                except Exception:
                    api_payload = {}
                episodes = (api_payload.get("data") or {}).get("epsodelist") or []
        videos: list[dict] = []
        for episode in episodes:
            title = str(episode.get("shortTitle") or episode.get("subtitle") or "").strip()
            url = self._normalize_iqiyi_url(str(episode.get("playUrl") or "").strip())
            if not title or not url:
                continue
            videos.append(
                {
                    "itemTitle": title,
                    "itemLink": url,
                    "itemNumber": self._to_int(episode.get("order")),
                    "tvId": episode.get("tvId"),
                    "albumId": payload.get("albumId") or album_info.get("albumId"),
                    "timeLength": self._parse_duration_seconds(episode.get("duration")),
                }
            )
        return videos

    def _album_avlist_api_url(self, url_param: str) -> str:
        value = str(url_param or "").strip()
        if not value:
            return ""
        if value.startswith(("http://", "https://")):
            return value
        if value.startswith("/"):
            return f"https://www.iqiyi.com{value}"
        return f"https://www.iqiyi.com/{value}"

    def _video_metadata(self, video: dict, album_info: dict) -> dict[str, str | int | None]:
        return {
            "tv_id": self._to_int(video.get("tvId") or video.get("qipu_id")),
            "album_id": self._to_int(video.get("albumId") or album_info.get("albumId")),
            "category_id": self._extract_category_id(album_info),
            "duration_seconds": self._to_int(video.get("timeLength")),
        }

    def _should_drop_search_item(self, item: dict) -> bool:
        album_info = item.get("albumDocInfo") or {}
        raw_score = album_info.get("douban_score")
        try:
            score = float(raw_score) if raw_score not in ("", None) else None
        except (TypeError, ValueError):
            score = None
        if score is not None and score < 2:
            return True
        channel = str(album_info.get("channel") or "")
        if any(keyword in channel for keyword in self._DROP_CHANNEL_KEYWORDS):
            return True
        if not album_info.get("itemTotalNumber"):
            return True
        title = str(album_info.get("albumTitle") or "")
        return any(keyword in title for keyword in self._DROP_TITLE_KEYWORDS)

    def _extract_page_info(self, html_text: str) -> dict:
        match = re.search(r"window\.Q\.PageInfo\.playPageInfo\s*=\s*(\{.*?\})\s*;", html_text, re.S)
        if match is None:
            raise DanmakuResolveError("爱奇艺页面缺少 playPageInfo")
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise DanmakuResolveError("爱奇艺页面 playPageInfo 解析失败") from exc

    def _try_extract_page_info(self, html_text: str) -> dict:
        try:
            return self._extract_page_info(html_text)
        except DanmakuResolveError:
            return {}

    def _resolve_danmaku_fields(self, page_info: dict, page_url: str) -> dict[str, str | int | None]:
        play_page_data = page_info.get("playPageData")
        if not isinstance(play_page_data, dict):
            play_page_data = {}
        cached = self._metadata_by_url.get(page_url) or self._metadata_by_url.get(self._swap_scheme(page_url))
        tv_id = str(page_info.get("tvId") or play_page_data.get("tvId") or (cached or {}).get("tv_id") or "").strip()
        return {
            "tv_id": tv_id,
            "album_id": page_info.get("albumId") or play_page_data.get("albumId") or (cached or {}).get("album_id"),
            "category_id": page_info.get("cid") or play_page_data.get("cid") or (cached or {}).get("category_id"),
        }

    def _remember_metadata(self, page_url: str, metadata: dict[str, str | int | None]) -> None:
        self._metadata_by_url[page_url] = dict(metadata)
        alternate = self._swap_scheme(page_url)
        if alternate != page_url:
            self._metadata_by_url[alternate] = dict(metadata)

    def _swap_scheme(self, page_url: str) -> str:
        if page_url.startswith("http://"):
            return "https://" + page_url[len("http://") :]
        if page_url.startswith("https://"):
            return "http://" + page_url[len("https://") :]
        return page_url

    def _normalize_iqiyi_url(self, page_url: str) -> str:
        value = str(page_url or "").strip()
        if value.startswith("http://") and "iqiyi.com" in value:
            return "https://" + value[len("http://") :]
        return value

    def _extract_category_id(self, album_info: dict) -> int | None:
        channel = str(album_info.get("channel") or "").strip()
        if not channel:
            return None
        tail = channel.rsplit(",", 1)[-1].strip()
        return self._to_int(tail)

    def _to_int(self, value) -> int | None:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _resolve_duration_seconds(self, page_info: dict, cached_metadata: dict[str, str | int | None] | None) -> int:
        duration = self._parse_duration_seconds(page_info.get("duration"))
        if duration > 0:
            return duration
        cached_duration = self._to_int((cached_metadata or {}).get("duration_seconds"))
        if cached_duration is not None and cached_duration > 0:
            return cached_duration
        return 0

    def _segment_url(self, tvid: str, page_index: int) -> str:
        return f"https://cmts.iqiyi.com/bullet/{tvid[-4:-2]}/{tvid[-2:]}/{tvid}_300_{page_index}.z"

    def _parse_duration_seconds(self, raw_duration) -> int:
        text = str(raw_duration or "").strip()
        if not text:
            return 0
        parts = text.split(":")
        try:
            values = [int(part) for part in parts]
        except ValueError:
            return 0
        if len(values) == 3:
            hours, minutes, seconds = values
            return hours * 3600 + minutes * 60 + seconds
        if len(values) == 2:
            minutes, seconds = values
            return minutes * 60 + seconds
        if len(values) == 1:
            return values[0]
        return 0

    def _parse_segment_records(self, xml_text: str, duration_seconds: int) -> list[DanmakuRecord]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise DanmakuResolveError("爱奇艺弹幕 XML 解析失败") from exc
        records: list[DanmakuRecord] = []
        for bullet in root.findall(".//bulletInfo"):
            content = (bullet.findtext("content") or "").strip()
            if not content:
                continue
            time_offset = self._parse_show_time_seconds(bullet.findtext("showTime"), duration_seconds)
            if time_offset is None:
                continue
            color = self._normalize_color(bullet.findtext("color"))
            records.append(DanmakuRecord(time_offset=time_offset, pos=1, color=color, content=content))
        return records

    def _parse_show_time_seconds(self, raw_show_time, duration_seconds: int) -> float | None:
        try:
            value = float(str(raw_show_time or "").strip())
        except ValueError:
            return None
        if value < 0:
            return None
        if duration_seconds > 0:
            if value > duration_seconds + 60:
                return round(value / 1000.0, 3)
            return round(value, 3)
        if value >= 1000:
            return round(value / 1000.0, 3)
        return round(value, 3)

    def _normalize_color(self, raw_color) -> str:
        try:
            return str(int(str(raw_color or "16777215").strip()))
        except ValueError:
            return "16777215"
