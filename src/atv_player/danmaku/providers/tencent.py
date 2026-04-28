from __future__ import annotations

import html
import json
import math
import re
from urllib.parse import parse_qs, quote, urlparse

import httpx

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem
from atv_player.danmaku.utils import extract_episode_number


class TencentDanmakuProvider:
    key = "tencent"
    _SEARCH_WALK_SKIP_KEYS = {"nestedDocs", "nestedBoxes", "richDocs"}
    _BARRAGE_BASE_URL = "https://dm.video.qq.com/barrage/base"
    _BARRAGE_SEGMENT_URL = "https://dm.video.qq.com/barrage/segment"
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
    _SEARCH_URL = "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch"
    _PAGE_DATA_URL = (
        "https://pbaccess.video.qq.com/trpc.universal_backend_service.page_server_rpc.PageServer/GetPageData"
        "?video_appid=3000010&vversion_name=8.2.96&vversion_platform=2"
    )
    _UNION_URL = "https://union.video.qq.com/fcgi-bin/data"
    _SEARCH_HEADERS = {
        "User-Agent": _UA_PC,
        "Accept": "application/json",
        "Origin": "https://v.qq.com",
        "Referer": "https://v.qq.com/",
        "trpc-trans-info": '{"trpc-env":""}',
    }
    _PAGE_DATA_HEADERS = {
        "User-Agent": _UA_MOBILE,
        "Accept": "application/json",
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

    def search(self, name: str, original_name: str | None = None) -> list[DanmakuSearchItem]:
        try:
            items = self._search_mb(self._search_keyword(name))
        except DanmakuSearchError:
            return []
        if not items:
            return []
        return self._expand_items_from_candidate_pages(name, items, original_name=original_name)

    def _expand_items_from_candidate_pages(
        self, query_name: str, items: list[DanmakuSearchItem], original_name: str | None = None
    ) -> list[DanmakuSearchItem]:
        requested_episode = extract_episode_number(original_name or query_name)
        if not items:
            return items
        if requested_episode is None and any(extract_episode_number(item.name) is not None for item in items):
            return items
        if requested_episode is not None and any(
            self._matches_requested_episode_item(item, requested_episode) for item in items
        ):
            return items
        expanded: list[DanmakuSearchItem] = []
        candidate_limit = 1 if requested_episode is None else 5
        for item in items[:candidate_limit]:
            page_data_items = self._fetch_page_data_episode_items(item.url, query_name)
            if page_data_items:
                expanded.extend(page_data_items)
                if requested_episode is None:
                    break
                if any(self._matches_requested_episode_item(candidate, requested_episode) for candidate in page_data_items):
                    break
            try:
                response = self._get(
                    item.url,
                    headers={"User-Agent": self._UA_PC, "Referer": "https://v.qq.com/"},
                    follow_redirects=True,
                    timeout=10.0,
                )
            except httpx.HTTPError:
                continue
            expanded.extend(self._extract_detail_episode_items(item.url, response.text, query_name))
            if requested_episode is None:
                break
            if any(self._matches_requested_episode_item(candidate, requested_episode) for candidate in expanded):
                break
        if not expanded:
            return items
        if requested_episode is None and not self._should_use_title_only_expansion(expanded):
            return items
        return self._merge_search_items(expanded, items)

    def _search_mb(self, name: str) -> list[DanmakuSearchItem]:
        params = {
            "q": name,
            "query": name,
            "vversion_platform": "2",
            "page_num": "1",
            "page_size": "20",
            "req_from": "web",
        }
        try:
            response = self._get(
                self._SEARCH_URL,
                params=params,
                headers=dict(self._SEARCH_HEADERS),
                follow_redirects=True,
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise DanmakuSearchError(f"腾讯搜索请求失败: {exc}") from exc
        try:
            data = response.json()
        except Exception as exc:
            raise DanmakuSearchError("腾讯搜索响应解析失败") from exc
        ret = data.get("ret")
        if ret not in (0, "0", None):
            raise DanmakuSearchError(f"腾讯搜索业务失败 ret={ret}")
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
                    found.append({"name": title, "url": url, "numeric_title_trusted": "1"})

        def walk(obj) -> None:
            if isinstance(obj, dict):
                maybe_url = ""
                maybe_title = ""
                for key, value in obj.items():
                    if str(key) in self._SEARCH_WALK_SKIP_KEYS:
                        continue
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

    def _extract_detail_episode_items(self, page_url: str, html_text: str, query_name: str) -> list[DanmakuSearchItem]:
        found: list[dict[str, str]] = []
        for episode in self._extract_json_array_by_key(html_text, "vsite_episode_list"):
            if not isinstance(episode, dict) or self._is_preview_episode_candidate(episode):
                continue
            url = str(episode.get("url") or episode.get("pageUrl") or episode.get("link") or "").replace("\\/", "/").strip()
            if not url:
                video_id = str(episode.get("vid") or episode.get("videoId") or episode.get("video_id") or "").strip()
                cover_id = self._extract_cover_id(page_url)
                if video_id and cover_id:
                    url = f"https://v.qq.com/x/cover/{cover_id}/{video_id}.html"
            title = self._clean_text(
                str(
                    episode.get("title")
                    or episode.get("playTitle")
                    or episode.get("play_title")
                    or episode.get("name")
                    or ""
                )
            )
            if url:
                found.append({"name": title, "url": url, "numeric_title_trusted": "1"})
        found.extend(self._extract_union_episode_items(page_url, html_text))
        found.extend(self._extract_html_episode_items(page_url, html_text))
        return self._to_search_items(self._prefer_main_episode_variants(self._dedupe_items(found)), query_name)

    def _fetch_page_data_episode_items(self, page_url: str, query_name: str) -> list[DanmakuSearchItem]:
        cover_id = self._extract_cover_id(page_url)
        if not cover_id:
            return []
        initial_context = (
            f"cid={cover_id}&detail_page_type=1&req_from=web_vsite&req_from_second_type=&req_type=0"
        )
        payload = self._request_page_data(cover_id, initial_context)
        if not payload:
            return []

        found = self._extract_page_data_items(payload, cover_id)
        tabs = self._extract_page_data_tabs(payload)
        seen_contexts = {initial_context}
        for page_context in self._page_data_followup_contexts(tabs):
            if not page_context or page_context in seen_contexts:
                continue
            seen_contexts.add(page_context)
            followup_payload = self._request_page_data(cover_id, page_context)
            if not followup_payload:
                continue
            found.extend(self._extract_page_data_items(followup_payload, cover_id))
        return self._to_search_items(self._prefer_main_episode_variants(self._dedupe_items(found)), query_name)

    def _request_page_data(self, cover_id: str, page_context: str) -> dict | None:
        payload = {
            "has_cache": 1,
            "page_params": {
                "req_from": "web_vsite",
                "page_id": "vsite_episode_list",
                "page_type": "detail_operation",
                "id_type": "1",
                "page_size": "",
                "cid": cover_id,
                "vid": "",
                "lid": "",
                "page_num": "",
                "page_context": page_context,
                "detail_page_type": "1",
            },
        }
        try:
            response = self._post(
                self._PAGE_DATA_URL,
                json=payload,
                headers=dict(self._PAGE_DATA_HEADERS),
                follow_redirects=True,
                timeout=10.0,
            )
        except httpx.HTTPError:
            return None
        try:
            data = response.json()
        except Exception:
            return None
        ret = data.get("ret")
        if ret not in (0, "0", None):
            return None
        return data if isinstance(data, dict) else None

    def _extract_page_data_tabs(self, payload: dict) -> list[dict]:
        for module_list_data in payload.get("data", {}).get("module_list_datas", []):
            if not isinstance(module_list_data, dict):
                continue
            for module_data in module_list_data.get("module_datas", []):
                if not isinstance(module_data, dict):
                    continue
                tabs_text = (module_data.get("module_params") or {}).get("tabs")
                if not tabs_text:
                    continue
                try:
                    tabs = json.loads(tabs_text)
                except json.JSONDecodeError:
                    continue
                if isinstance(tabs, list):
                    return [tab for tab in tabs if isinstance(tab, dict)]
        return []

    def _page_data_followup_contexts(self, tabs: list[dict]) -> list[str]:
        if not tabs:
            return []
        selected_contexts = {
            str(tab.get("page_context") or "")
            for tab in tabs
            if self._is_page_data_tab_selected(tab) and str(tab.get("page_context") or "")
        }
        contexts: list[str] = []
        for index, tab in enumerate(tabs):
            page_context = str(tab.get("page_context") or "").strip()
            if not page_context:
                continue
            if selected_contexts:
                if page_context in selected_contexts:
                    continue
            elif index == 0:
                continue
            contexts.append(page_context)
        return contexts

    def _is_page_data_tab_selected(self, tab: dict) -> bool:
        selected = tab.get("selected")
        if isinstance(selected, bool):
            return selected
        return str(selected).strip().lower() in {"1", "true"}

    def _extract_page_data_items(self, payload: dict, cover_id: str) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        for module_list_data in payload.get("data", {}).get("module_list_datas", []):
            if not isinstance(module_list_data, dict):
                continue
            for module_data in module_list_data.get("module_datas", []):
                if not isinstance(module_data, dict):
                    continue
                item_datas = ((module_data.get("item_data_lists") or {}).get("item_datas") or [])
                for item in item_datas:
                    if not isinstance(item, dict):
                        continue
                    params = item.get("item_params") or {}
                    candidate = self._page_data_item_to_episode(params, cover_id)
                    if candidate is not None:
                        found.append(candidate)
        return found

    def _page_data_item_to_episode(self, params: dict, cover_id: str) -> dict[str, str] | None:
        vid = str(params.get("vid") or "").strip()
        if not vid:
            return None
        title = self._clean_text(str(params.get("title") or ""))
        play_title = self._clean_text(str(params.get("play_title") or ""))
        union_title = self._clean_text(str(params.get("union_title") or ""))
        episode_title = play_title or title or union_title
        if self._is_page_data_preview_candidate(params, episode_title):
            return None
        episode_no = (
            extract_episode_number(play_title)
            or extract_episode_number(title)
            or extract_episode_number(union_title)
        )
        if episode_no is None:
            return None
        return {
            "name": title or episode_title,
            "url": f"https://v.qq.com/x/cover/{cover_id}/{vid}.html",
            "episode_no": episode_no,
            "is_preview": False,
            "duration": str(params.get("duration") or ""),
            "numeric_title_trusted": "1",
        }

    def _is_page_data_preview_candidate(self, params: dict, title: str) -> bool:
        if str(params.get("is_trailer") or "").strip() == "1":
            return True
        if title and not self._is_main_content_title(title):
            return True
        return self._is_preview_episode_candidate(
            {
                "title": title,
                "markLabel": str(params.get("imgtag_all") or ""),
                "rawTags": str(params.get("uni_imgtag") or ""),
                "titleSuffix": str(params.get("video_subtitle") or ""),
            }
        )

    def _extract_html_episode_items(self, page_url: str, html_text: str) -> list[dict[str, str]]:
        cover_id = self._extract_cover_id(page_url)
        if not cover_id:
            return []
        pattern = re.compile(
            r'<div(?P<attrs>[^>]*class="[^"]*\bepisode-item\b[^"]*"[^>]*)>'
            r'(?:(?!<div[^>]*class="[^"]*\bepisode-item\b).)*?'
            r'<span[^>]*class="[^"]*\bepisode-item-text\b[^"]*"[^>]*>(?P<episode>[^<]+)</span>',
            re.IGNORECASE | re.S,
        )
        found: list[dict[str, str]] = []
        for match in pattern.finditer(html_text):
            vid = self._extract_episode_item_vid(match.group("attrs"))
            episode = self._clean_text(match.group("episode"))
            if not vid or not episode:
                continue
            found.append(
                {
                    "name": episode,
                    "url": f"https://v.qq.com/x/cover/{cover_id}/{vid}.html",
                    "numeric_title_trusted": "1",
                }
            )
        return found

    def _extract_json_array_by_key(self, text: str, key: str) -> list:
        search_from = 0
        token = f'"{key}"'
        while True:
            key_index = text.find(token, search_from)
            if key_index < 0:
                return []
            array_start = text.find("[", key_index)
            if array_start < 0:
                return []
            payload = self._extract_balanced_block(text, array_start, "[", "]")
            if not payload:
                return []
            try:
                value = json.loads(payload)
            except json.JSONDecodeError:
                search_from = key_index + len(token)
                continue
            if isinstance(value, list):
                return value
            search_from = key_index + len(token)

    def _extract_js_array_by_key(self, text: str, key: str) -> list:
        tokens = (f'"{key}"', f"{key}:")
        for token in tokens:
            search_from = 0
            while True:
                key_index = text.find(token, search_from)
                if key_index < 0:
                    break
                array_start = text.find("[", key_index)
                if array_start < 0:
                    break
                payload = self._extract_balanced_block(text, array_start, "[", "]")
                if not payload:
                    break
                try:
                    value = json.loads(payload)
                except json.JSONDecodeError:
                    search_from = key_index + len(token)
                    continue
                if isinstance(value, list):
                    return value
                search_from = key_index + len(token)
        return []

    def _extract_balanced_block(self, text: str, start: int, open_char: str, close_char: str) -> str:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == open_char:
                depth += 1
            elif char == close_char:
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        return ""

    def _web_search_keyword(self, name: str) -> str:
        candidate = name.strip()
        stripped = re.sub(r"\s+第?\d+\s*集\s*$", "", candidate, flags=re.IGNORECASE)
        stripped = re.sub(r"\s+S\d+\s*E\d+(?:[\s._-].*)?$", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s+EP?\d+(?:[\s._-].*)?$", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s+E\d+(?:[\s._-].*)?$", "", stripped, flags=re.IGNORECASE)
        return stripped.strip() or candidate

    def _search_keyword(self, name: str) -> str:
        if extract_episode_number(name) is not None:
            return self._web_search_keyword(name)
        return name.strip()

    def _to_search_items(self, items: list[dict[str, str]], query_name: str) -> list[DanmakuSearchItem]:
        episode_keyword_base = self._episode_keyword_base(query_name)
        output: list[DanmakuSearchItem] = []
        for item in items:
            raw_name = item.get("name", "").strip()
            url = item.get("url", "").strip()
            if not raw_name or not url or not url.startswith("https://v.qq.com/"):
                continue
            match = re.fullmatch(r"(\d+)", raw_name)
            if match is not None and episode_keyword_base and self._numeric_title_is_trusted(item):
                raw_name = f"{episode_keyword_base} {match.group(1)}集"
            duration_text = str(item.get("duration") or "0").strip()
            duration_seconds = int(duration_text) if duration_text.isdigit() else 0
            output.append(
                DanmakuSearchItem(provider=self.key, name=raw_name, url=url, duration_seconds=duration_seconds)
            )
        return output

    def _episode_keyword_base(self, query_name: str) -> str:
        return re.sub(r"\s+第?\d+\s*集\s*$", "", query_name.strip(), flags=re.IGNORECASE).strip()

    def _numeric_title_is_trusted(self, item: dict[str, str]) -> bool:
        return str(item.get("numeric_title_trusted") or "").strip() == "1"

    def _matches_requested_episode_item(self, item: DanmakuSearchItem, requested_episode: int) -> bool:
        if extract_episode_number(item.name) != requested_episode:
            return False
        return re.fullmatch(r"\d+", item.name.strip()) is None

    def _should_use_title_only_expansion(self, items: list[DanmakuSearchItem]) -> bool:
        episode_numbers: list[int] = []
        for item in items:
            episode_number = extract_episode_number(item.name)
            if episode_number is None:
                return False
            episode_numbers.append(episode_number)
        unique_numbers = sorted(set(episode_numbers))
        if len(unique_numbers) < 2 or len(unique_numbers) > 4:
            return False
        return unique_numbers == list(range(1, len(unique_numbers) + 1))

    def _extract_cover_id(self, page_url: str) -> str:
        parsed = urlparse(page_url)
        match = re.search(r"/x/cover(?:_seo)?/([^/]+)/", parsed.path)
        if match is not None:
            return match.group(1)
        return ""

    def _extract_episode_item_vid(self, attrs: str) -> str:
        params_match = re.search(r'dt-params="([^"]+)"', attrs)
        if params_match is not None:
            params = parse_qs(html.unescape(params_match.group(1)))
            vid = params.get("vid", [""])[0].strip()
            if vid:
                return vid
        return self._match_first(
            attrs,
            (
                r'data-vid="([\w]+)"',
                r'id="video-[^"]+?_([\w]+)_[^"]*"',
            ),
        )

    def _merge_search_items(
        self, primary: list[DanmakuSearchItem], secondary: list[DanmakuSearchItem]
    ) -> list[DanmakuSearchItem]:
        seen: set[str] = set()
        output: list[DanmakuSearchItem] = []
        for item in [*primary, *secondary]:
            if not item.url or item.url in seen:
                continue
            seen.add(item.url)
            output.append(item)
        return output

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

    def _prefer_main_episode_variants(self, items: list[dict[str, str]]) -> list[dict[str, str]]:
        best_by_episode: dict[int, dict[str, str]] = {}
        for item in items:
            episode_no = self._episode_number_from_item(item)
            if episode_no is None:
                continue
            current = best_by_episode.get(episode_no)
            if current is None or self._episode_item_priority(item) > self._episode_item_priority(current):
                best_by_episode[episode_no] = item

        emitted_episodes: set[int] = set()
        output: list[dict[str, str]] = []
        for item in items:
            episode_no = self._episode_number_from_item(item)
            if episode_no is None:
                output.append(item)
                continue
            if episode_no in emitted_episodes:
                continue
            best = best_by_episode.get(episode_no)
            if best is None or best.get("url") != item.get("url"):
                continue
            emitted_episodes.add(episode_no)
            output.append(item)
        return output

    def _episode_number_from_item(self, item: dict[str, str]) -> int | None:
        episode_no = item.get("episode_no")
        if isinstance(episode_no, int):
            return episode_no
        return extract_episode_number(str(item.get("name") or ""))

    def _episode_item_priority(self, item: dict[str, str]) -> tuple[int, int]:
        is_preview = item.get("is_preview")
        if not isinstance(is_preview, bool):
            is_preview = self._is_preview_episode_candidate(
                {
                    "title": str(item.get("name") or ""),
                    "markLabel": str(item.get("markLabel") or ""),
                    "rawTags": str(item.get("rawTags") or ""),
                    "titleSuffix": str(item.get("titleSuffix") or ""),
                }
            )
        duration_text = str(item.get("duration") or "0").strip()
        duration = int(duration_text) if duration_text.isdigit() else 0
        return (0 if is_preview else 1, duration)

    def _extract_union_episode_items(self, page_url: str, html_text: str) -> list[dict[str, str]]:
        cover_id = self._extract_cover_id(page_url)
        if not cover_id:
            return []
        video_ids = [str(value).strip() for value in self._extract_js_array_by_key(html_text, "video_ids") if str(value).strip()]
        if not video_ids:
            return []
        found: list[dict[str, str]] = []
        for start in range(0, len(video_ids), 30):
            chunk = video_ids[start : start + 30]
            params = {
                "otype": "json",
                "tid": "1804",
                "appid": "20001238",
                "appkey": "6c03bbe9658448a4",
                "union_platform": "1",
                "idlist": ",".join(chunk),
            }
            try:
                response = self._get(
                    self._UNION_URL,
                    params=params,
                    headers={"User-Agent": self._UA_PC, "Referer": "https://v.qq.com/"},
                    follow_redirects=True,
                    timeout=10.0,
                )
            except httpx.HTTPError:
                continue
            payload = self._parse_union_payload(response.text)
            if not isinstance(payload, dict):
                continue
            for result in payload.get("results", []):
                if not isinstance(result, dict) or int(result.get("retcode") or 0) != 0:
                    continue
                fields = result.get("fields") if isinstance(result.get("fields"), dict) else {}
                vid = str(fields.get("vid") or result.get("id") or "").strip()
                item_cover_id = str(fields.get("c_covers") or cover_id).strip()
                title = self._clean_text(
                    str(fields.get("c_title_output") or fields.get("c_title_detail") or fields.get("title") or "")
                )
                if not vid or not item_cover_id or not title:
                    continue
                found.append(
                    {
                        "name": title,
                        "url": f"https://v.qq.com/x/cover/{item_cover_id}/{vid}.html",
                        "episode_no": extract_episode_number(title) or extract_episode_number(str(fields.get("title") or "")),
                        "is_preview": self._is_union_preview_candidate(fields),
                        "duration": str(fields.get("duration") or ""),
                        "numeric_title_trusted": "1",
                    }
                )
        return self._prefer_main_episode_variants(self._dedupe_items(found))

    def _parse_union_payload(self, text: str) -> dict | None:
        payload = text.strip()
        if payload.startswith("QZOutputJson="):
            payload = payload[len("QZOutputJson=") :]
        if payload.endswith(";"):
            payload = payload[:-1]
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _is_union_preview_candidate(self, fields: dict) -> bool:
        title = self._clean_text(
            str(fields.get("c_title_output") or fields.get("c_title_detail") or fields.get("title") or "")
        )
        category_parts = fields.get("category_map") if isinstance(fields.get("category_map"), list) else []
        category_text = " ".join(str(part) for part in category_parts)
        return self._is_preview_episode_candidate(
            {
                "title": title,
                "markLabel": category_text,
                "rawTags": category_text,
                "titleSuffix": str(fields.get("positive_trailer") or ""),
            }
        )

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
        segment_urls, used_base_segment_index = self._resolve_segment_urls(video_id, duration)
        records: list[DanmakuRecord] = []
        seen: set[tuple[float, str]] = set()
        for segment_index, segment_url in enumerate(segment_urls):
            segment_response = self._get(
                segment_url,
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
                if not used_base_segment_index and segment_index > 0:
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

    def _resolve_segment_urls(self, video_id: str, duration: int) -> tuple[list[str], bool]:
        segment_urls = self._segment_urls_from_base(video_id)
        if segment_urls:
            return segment_urls, True
        return self._segment_urls_from_duration(video_id, duration), False

    def _segment_urls_from_base(self, video_id: str) -> list[str]:
        try:
            response = self._get(
                f"{self._BARRAGE_BASE_URL}/{quote(video_id)}",
                headers={
                    "User-Agent": self._UA_PC,
                    "Referer": "https://v.qq.com/",
                    "Accept": "application/json,text/plain,*/*",
                },
                follow_redirects=True,
                timeout=10.0,
            )
        except httpx.HTTPError:
            return []
        if response.status_code == 404:
            return []
        try:
            payload = response.json()
        except Exception:
            return []
        segment_index = payload.get("segment_index")
        if not isinstance(segment_index, dict):
            return []

        segment_rows: list[tuple[int, str]] = []
        for item in segment_index.values():
            if not isinstance(item, dict):
                continue
            segment_name = str(item.get("segment_name") or "").strip().lstrip("/")
            if not segment_name:
                continue
            segment_start = item.get("segment_start")
            try:
                sort_key = int(segment_start)
            except (TypeError, ValueError):
                sort_key = self._segment_sort_key_from_name(segment_name)
            segment_rows.append((sort_key, f"{self._BARRAGE_SEGMENT_URL}/{quote(video_id)}/{segment_name}"))

        segment_rows.sort(key=lambda row: row[0])
        return [url for _, url in segment_rows]

    def _segment_urls_from_duration(self, video_id: str, duration: int) -> list[str]:
        segment_count = max(1, math.ceil(duration / 30)) if duration else 8
        segment_urls: list[str] = []
        for segment in range(segment_count):
            start_ms = segment * 30000
            end_ms = start_ms + 30000
            segment_urls.append(f"{self._BARRAGE_SEGMENT_URL}/{quote(video_id)}/t/v1/{start_ms}/{end_ms}")
        return segment_urls

    def _segment_sort_key_from_name(self, segment_name: str) -> int:
        parts = [part for part in segment_name.split("/") if part]
        if len(parts) >= 2 and parts[-2].isdigit():
            return int(parts[-2])
        return 0

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
