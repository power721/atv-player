from __future__ import annotations

import html
import json
import math
import re
from urllib.parse import parse_qs, quote, urlparse

import httpx

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem


class TencentDanmakuProvider:
    key = "tencent"
    _NON_MAIN_CONTENT_KEYWORDS = (
        "：",
        "#",
        "特辑",
        '"',
        "预告",
        "预告片",
        "剪辑",
        "片花",
        "独家",
        "专访",
        "纯享",
        "旁白版",
        "制作",
        "幕后",
        "宣传",
        "MV",
        "主题曲",
        "插曲",
        "彩蛋",
        "精彩",
        "集锦",
        "盘点",
        "回顾",
        "解说",
        "评测",
        "反应",
        "reaction",
    )
    _UA_PC = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    )
    _UA_MOBILE = (
        "Mozilla/5.0 (Linux; Android 13; M2104K10AC Build/TP1A.220624.014) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/131.0.6778.200 "
        "Mobile Safari/537.36"
    )
    _SEARCH_URL = (
        "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch?vplatform=2"
    )
    _SEARCH_HEADERS = {
        "User-Agent": _UA_MOBILE,
        "Content-Type": "application/json",
        "Origin": "https://v.qq.com",
        "Referer": "https://v.qq.com/",
    }
    _WEB_SEARCH_HEADERS = {
        "User-Agent": _UA_PC,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Referer": "https://v.qq.com/",
    }

    def __init__(self, get=httpx.get, post=httpx.post) -> None:
        self._get = get
        self._post = post

    def supports(self, page_url: str) -> bool:
        return "qq.com" in page_url

    def search(self, name: str) -> list[DanmakuSearchItem]:
        mobile_error: DanmakuSearchError | None = None
        try:
            items = self._search_mb(name)
        except DanmakuSearchError as exc:
            mobile_error = exc
            items = []
        if items:
            return items
        web_keyword = self._web_search_keyword(name)
        try:
            return self._search_web(web_keyword)
        except DanmakuSearchError:
            if mobile_error is not None:
                raise mobile_error
            raise

    def _search_mb(self, name: str) -> list[DanmakuSearchItem]:
        payload = {
            "version": "25042201",
            "clientType": 1,
            "filterValue": "",
            "uuid": "B1E50847-D25F-4C4B-BBA0-36F0093487F6",
            "retry": 0,
            "query": name,
            "pagenum": 0,
            "isPrefetch": True,
            "pagesize": 30,
            "queryFrom": 0,
            "searchDatakey": "",
            "transInfo": "",
            "isneedQc": True,
            "preQid": "",
            "adClientInfo": "",
            "extraInfo": {
                "isNewMarkLabel": "1",
                "multi_terminal_pc": "1",
                "themeType": "1",
                "sugRelatedIds": "{}",
                "appVersion": "",
            },
        }
        try:
            response = self._post(
                self._SEARCH_URL,
                content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                headers=dict(self._SEARCH_HEADERS),
                follow_redirects=True,
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise DanmakuSearchError(f"腾讯移动搜索请求失败: {exc}") from exc
        try:
            data = response.json()
        except Exception as exc:
            raise DanmakuSearchError("腾讯移动搜索响应解析失败") from exc
        ret = data.get("ret")
        if ret not in (0, "0", None):
            raise DanmakuSearchError(f"腾讯移动搜索业务失败 ret={ret}")
        return self._extract_mb_items(data, name)

    def _extract_mb_items(self, data: dict, query_name: str) -> list[DanmakuSearchItem]:
        found: list[dict[str, str]] = []

        for item in data.get("data", {}).get("normalList", {}).get("itemList", []):
            video_info = item.get("videoInfo") or {}
            if not self._is_main_content_title(str(video_info.get("title") or "")):
                continue
            if not self._is_qq_platform(video_info.get("playSites")):
                continue
            if not self._has_episode_sites(video_info.get("episodeSites")):
                continue
            episode_info_list = video_info.get("episodeInfoList") or []
            for episode in episode_info_list:
                if self._is_preview_episode_candidate(episode):
                    continue
                url = str(episode.get("url") or "").replace("\\/", "/").strip()
                title = self._clean_text(str(episode.get("title") or ""))
                if url and title:
                    found.append({"name": title, "url": url})

        def walk(obj) -> None:
            if isinstance(obj, dict):
                maybe_url = ""
                maybe_title = ""
                for key, value in obj.items():
                    normalized_key = str(key).lower()
                    if isinstance(value, str):
                        if "v.qq.com" in value:
                            maybe_url = value.replace("\\/", "/")
                        if normalized_key in ("title", "name", "text", "video_title"):
                            maybe_title = self._clean_text(value)
                    walk(value)
                if maybe_url and maybe_title and not self._is_preview_episode_candidate(obj):
                    found.append({"name": maybe_title, "url": maybe_url})
                return
            if isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(data)
        return self._to_search_items(self._dedupe_items(found), query_name)

    def _is_main_content_title(self, title: str) -> bool:
        candidate = str(title or "")
        if not candidate or "<em>" in candidate or "</em>" in candidate:
            return False
        lowered = candidate.casefold()
        return not any(keyword.casefold() in lowered for keyword in self._NON_MAIN_CONTENT_KEYWORDS)

    def _is_qq_platform(self, play_sites) -> bool:
        if not isinstance(play_sites, list) or not play_sites:
            return True
        for site in play_sites:
            if not isinstance(site, dict):
                continue
            if str(site.get("enName") or "").strip().lower() == "qq":
                return True
        return False

    def _has_episode_sites(self, episode_sites) -> bool:
        if not isinstance(episode_sites, dict):
            return False
        return bool(episode_sites)

    def _is_preview_episode_candidate(self, episode: dict) -> bool:
        marker_text = " ".join(
            str(episode.get(key) or "")
            for key in ("markLabel", "rawTags", "titleSuffix")
        )
        if any(keyword in marker_text for keyword in ("预告", '"text":"预"', "预告片")):
            return True
        title = self._clean_text(str(episode.get("title") or ""))
        return not self._is_main_content_title(title)

    def _search_web(self, keyword: str) -> list[DanmakuSearchItem]:
        try:
            response = self._get(
                f"https://v.qq.com/x/search/?q={quote(keyword)}",
                headers=dict(self._WEB_SEARCH_HEADERS),
                follow_redirects=True,
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise DanmakuSearchError(f"腾讯网页搜索请求失败: {exc}") from exc
        text = response.text
        found: list[dict[str, str]] = []
        patterns = (
            r'<a[^>]+href="(https://v\.qq\.com/x/cover/[^"]+)"[^>]*title="([^"]+)"',
            r'<a[^>]+href="(https://v\.qq\.com/x/cover/[^"]+)"[^>]*>(.*?)</a>',
        )
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.S):
                url = match.group(1).replace("\\/", "/")
                title = self._clean_text(match.group(2)) if len(match.groups()) >= 2 else ""
                if url:
                    found.append({"name": title or keyword, "url": url})
        return self._to_search_items(self._dedupe_items(found), keyword)

    def _web_search_keyword(self, name: str) -> str:
        candidate = name.strip()
        stripped = re.sub(r"\s+第?\d+\s*集\s*$", "", candidate, flags=re.IGNORECASE)
        stripped = re.sub(r"\s+S\d+\s*E\d+(?:[\s._-].*)?$", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s+EP?\d+(?:[\s._-].*)?$", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s+E\d+(?:[\s._-].*)?$", "", stripped, flags=re.IGNORECASE)
        return stripped.strip() or candidate

    def _to_search_items(self, items: list[dict[str, str]], query_name: str) -> list[DanmakuSearchItem]:
        episode_keyword_base = self._episode_keyword_base(query_name)
        output: list[DanmakuSearchItem] = []
        for item in items:
            raw_name = item.get("name", "").strip()
            url = item.get("url", "").strip()
            if not raw_name or not url or not url.startswith("https://v.qq.com/"):
                continue
            match = re.fullmatch(r"(\d+)", raw_name)
            if match is not None and episode_keyword_base:
                raw_name = f"{episode_keyword_base} {match.group(1)}集"
            output.append(DanmakuSearchItem(provider=self.key, name=raw_name, url=url))
        return output

    def _episode_keyword_base(self, query_name: str) -> str:
        return re.sub(r"\s+\d+\s*集\s*$", "", query_name.strip(), flags=re.IGNORECASE).strip()

    def _dedupe_items(self, items: list[dict[str, str]]) -> list[dict[str, str]]:
        seen: set[str] = set()
        output: list[dict[str, str]] = []
        for item in items:
            url = item.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            output.append(item)
        return output

    def _clean_text(self, value: str) -> str:
        without_tags = re.sub(r"<.*?>", "", value)
        return html.unescape(without_tags).strip()

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        response = self._get(
            page_url,
            headers={"User-Agent": self._UA_PC, "Referer": "https://v.qq.com/"},
            follow_redirects=True,
            timeout=10.0,
        )
        video_id = self._extract_video_id(page_url, response.text)
        if not video_id:
            raise DanmakuResolveError("腾讯页面缺少 videoId/vid")
        duration = self._extract_duration_seconds(response.text)
        segment_count = max(1, math.ceil(duration / 30)) if duration else 8
        records: list[DanmakuRecord] = []
        seen: set[tuple[float, str]] = set()
        for segment in range(segment_count):
            start_ms = segment * 30000
            end_ms = start_ms + 30000
            segment_response = self._get(
                f"https://dm.video.qq.com/barrage/segment/{quote(video_id)}/t/v1/{start_ms}/{end_ms}",
                headers={
                    "User-Agent": self._UA_PC,
                    "Referer": "https://v.qq.com/",
                    "Accept": "application/json,text/plain,*/*",
                },
                follow_redirects=True,
                timeout=10.0,
            )
            try:
                payload = segment_response.json()
            except Exception:
                continue
            barrage_list = payload.get("barrage_list") or []
            if not barrage_list:
                if segment > 0:
                    break
                continue
            for item in barrage_list:
                content = str(item.get("content") or "").strip()
                time_offset = round(float(item.get("time_offset") or 0.0) / 1000, 3)
                style = item.get("content_style") if isinstance(item.get("content_style"), dict) else {}
                key = (time_offset, content)
                if not content or key in seen:
                    continue
                seen.add(key)
                records.append(
                    DanmakuRecord(
                        time_offset=time_offset,
                        pos=int(style.get("position") or 1),
                        color=str(style.get("color") or 16777215),
                        content=content,
                    )
                )
        return records

    def _extract_video_id(self, page_url: str, html_text: str) -> str:
        video_id = self._match_first(
            html_text,
            (
                r'"videoId":"([^"]+)"',
                r'"vid":"([^"]+)"',
                r'"vid"\s*:\s*"([^"]+)"',
                r'https://m\.v\.qq\.com/x/m/play\?[^"\']*vid=([\w]+)',
            ),
        )
        if video_id:
            return video_id
        parsed = urlparse(page_url)
        query_vid = parse_qs(parsed.query).get("vid", [""])[0].strip()
        if query_vid:
            return query_vid
        cover_match = re.search(r"/x/cover(?:_seo)?/[^/]+/([\w]+)\.html", parsed.path)
        if cover_match is not None:
            return cover_match.group(1)
        page_match = re.search(r"/x/page(?:_seo)?/([\w]+)\.html", parsed.path)
        if page_match is not None:
            return page_match.group(1)
        return ""

    def _extract_duration_seconds(self, html_text: str) -> int:
        duration_text = self._match_first(
            html_text,
            (
                r'"duration":"(\d+)"',
                r'"duration":(\d+)',
                r'<meta\s+property="video:duration"\s+content="(\d+)"',
                r'<meta\s+itemprop="duration"\s+content="(\d+)"',
            ),
        )
        if duration_text and duration_text.isdigit():
            return int(duration_text)
        return 0

    def _match_first(self, text: str, patterns: tuple[str, ...]) -> str:
        for pattern in patterns:
            match = re.search(pattern, text, re.S)
            if match is not None:
                return match.group(1)
        return ""
