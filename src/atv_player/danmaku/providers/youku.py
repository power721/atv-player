from __future__ import annotations

import base64
import html
import hashlib
import json
import math
import re
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem
from atv_player.danmaku.utils import extract_episode_number, normalize_name, strip_episode_suffix


class YoukuDanmakuProvider:
    key = "youku"
    _OPENAPI_VIDEO_INFO_URL = "https://openapi.youku.com/v2/videos/show.json"
    _CNA_URL = "https://log.mmstat.com/eg.js"
    _WEAKGET_URL = "https://acs.youku.com/h5/mtop.com.youku.aplatform.weakget/1.0/?jsv=2.5.1&appKey=24679788"
    _SIGNED_DANMAKU_URL = "https://acs.youku.com/h5/mopen.youku.danmu.list/1.0/"
    _OPENAPI_CLIENT_ID = "53e6cc67237fc59a"
    _OPENAPI_PACKAGE = "com.huawei.hwvplayer.youku"
    _APP_KEY = "24679788"
    _MESSAGE_SECRET = "MkmC9SoIw6xCkSKHhJ7b5D2r51kBiREr"
    _SEGMENT_SECONDS = 60
    _SEARCH_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    )

    def __init__(self, get=httpx.get, post=httpx.post) -> None:
        self._get = get
        self._post = post

    def supports(self, page_url: str) -> bool:
        return "youku.com" in page_url

    def search(self, name: str, original_name: str | None = None) -> list[DanmakuSearchItem]:
        response = self._get(
            "https://search.youku.com/api/search",
            params={
                "keyword": name,
                "userAgent": self._SEARCH_USER_AGENT,
                "site": 1,
                "categories": 0,
                "ftype": 0,
                "ob": 0,
                "pg": 1,
            },
            headers={
                "user-agent": self._SEARCH_USER_AGENT,
                "accept": "application/json",
                "referer": "https://www.youku.com/",
            },
            follow_redirects=True,
            timeout=10.0,
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise DanmakuSearchError("优酷弹幕搜索结果解析失败") from exc
        items = self._extract_search_items(payload, name)
        return self._expand_items_from_candidate_pages(items)

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        response = self._get(page_url, headers={"user-agent": "Mozilla/5.0"}, follow_redirects=True, timeout=10.0)
        vid = self._extract_video_id(page_url, response.text)
        if not vid:
            raise DanmakuResolveError("优酷页面缺少 vid")
        duration = self._fetch_duration_seconds(vid)
        cna, tk, tk_enc = self._fetch_danmaku_tokens()
        if not cna or not tk or not tk_enc:
            raise DanmakuResolveError("优酷弹幕鉴权失败")
        items = self._fetch_all_danmaku_items(page_url, vid, duration, cna, tk, tk_enc)
        records: list[DanmakuRecord] = []
        for item in items:
            raw_properties = item.get("propertis") or "{}"
            properties = json.loads(raw_properties) if isinstance(raw_properties, str) else dict(raw_properties)
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            records.append(
                DanmakuRecord(
                    time_offset=round(float(item.get("playat") or 0) / 1000, 3),
                    pos=int(properties.get("pos") or 1),
                    color=str(properties.get("color") or 16777215),
                    content=content,
                )
            )
        return records

    def _extract_video_id(self, page_url: str, html_text: str) -> str:
        match = self._match_first(
            html_text,
            (
                r'"vid":"([^"]+)"',
                r'"videoId":"([^"]+)"',
                r'"videoId"\s*:\s*"([^"]+)"',
            ),
        )
        if match:
            return match
        parsed = urlparse(page_url)
        path_match = re.search(r"/v_show/id_([^/.]+)\.html", parsed.path)
        if path_match is not None:
            return path_match.group(1)
        return parse_qs(parsed.query).get("vid", [""])[0].strip()

    def _fetch_duration_seconds(self, vid: str) -> int:
        response = self._get(
            (
                f"{self._OPENAPI_VIDEO_INFO_URL}?client_id={self._OPENAPI_CLIENT_ID}"
                f"&video_id={vid}&package={self._OPENAPI_PACKAGE}&ext=show"
            ),
            headers={"user-agent": self._SEARCH_USER_AGENT},
            follow_redirects=True,
            timeout=10.0,
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise DanmakuResolveError("优酷视频信息解析失败") from exc
        duration = payload.get("duration")
        try:
            return max(1, math.ceil(float(duration or 0)))
        except (TypeError, ValueError):
            raise DanmakuResolveError("优酷视频信息缺少时长")

    def _fetch_danmaku_tokens(self) -> tuple[str, str, str]:
        cna_response = self._get(
            self._CNA_URL,
            headers={"user-agent": self._SEARCH_USER_AGENT},
            follow_redirects=False,
            timeout=10.0,
        )
        cna = str(cna_response.headers.get("etag") or "").strip().strip('"')
        weakget_response = self._get(
            self._WEAKGET_URL,
            headers={"user-agent": self._SEARCH_USER_AGENT},
            follow_redirects=False,
            timeout=10.0,
        )
        cookie_text = str(weakget_response.headers.get("set-cookie") or "")
        tk_match = re.search(r"_m_h5_tk=([^;]+)", cookie_text)
        tk_enc_match = re.search(r"_m_h5_tk_enc=([^;]+)", cookie_text)
        return (
            cna,
            tk_match.group(1) if tk_match else "",
            tk_enc_match.group(1) if tk_enc_match else "",
        )

    def _fetch_all_danmaku_items(
        self, page_url: str, vid: str, duration_seconds: int, cna: str, tk: str, tk_enc: str
    ) -> list[dict]:
        segment_count = max(1, math.ceil(duration_seconds / self._SEGMENT_SECONDS))
        items: list[dict] = []
        failure_count = 0
        for mat in range(segment_count):
            try:
                items.extend(self._fetch_segment_items(page_url, vid, mat, cna, tk, tk_enc))
            except httpx.HTTPError:
                failure_count += 1
        if not items and failure_count >= segment_count:
            raise DanmakuResolveError("优酷弹幕分段请求失败")
        return items

    def _fetch_segment_items(self, page_url: str, vid: str, mat: int, cna: str, tk: str, tk_enc: str) -> list[dict]:
        message = {
            "ctime": int(time.time() * 1000),
            "ctype": 10004,
            "cver": "v1.0",
            "guid": cna,
            "mat": mat,
            "mcount": 1,
            "pid": 0,
            "sver": "3.1.0",
            "type": 1,
            "vid": vid,
        }
        message_b64 = base64.b64encode(json.dumps(message, separators=(",", ":")).encode()).decode()
        message["msg"] = message_b64
        message["sign"] = hashlib.md5(f"{message_b64}{self._MESSAGE_SECRET}".encode()).hexdigest().lower()
        data_text = json.dumps(message, separators=(",", ":"))
        ts = str(int(time.time() * 1000))
        sign_source = f"{tk[:32]}&{ts}&{self._APP_KEY}&{data_text}"
        params = {
            "jsv": "2.5.6",
            "appKey": self._APP_KEY,
            "t": ts,
            "sign": hashlib.md5(sign_source.encode()).hexdigest().lower(),
            "api": "mopen.youku.danmu.list",
            "v": "1.0",
            "type": "originaljson",
            "dataType": "jsonp",
            "timeout": "20000",
            "jsonpIncPrefix": "utility",
        }
        response = self._post(
            self._SIGNED_DANMAKU_URL,
            params=params,
            data={"data": data_text},
            headers={
                "user-agent": self._SEARCH_USER_AGENT,
                "referer": page_url,
                "content-type": "application/x-www-form-urlencoded",
                "cookie": f"_m_h5_tk={tk}; _m_h5_tk_enc={tk_enc};",
            },
            follow_redirects=True,
            timeout=10.0,
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise DanmakuResolveError("优酷弹幕响应解析失败") from exc
        result_text = ((payload.get("data") or {}).get("result") or "")
        if not result_text:
            return []
        try:
            result_payload = json.loads(result_text)
        except json.JSONDecodeError as exc:
            raise DanmakuResolveError("优酷弹幕数据解析失败") from exc
        if str(result_payload.get("code")) == "-1":
            return []
        return list(((result_payload.get("data") or {}).get("result") or []))

    def _extract_search_items(self, payload: dict, query_name: str) -> list[DanmakuSearchItem]:
        if isinstance(payload.get("pageComponentList"), list):
            return self._extract_page_component_items(payload["pageComponentList"])
        if isinstance(payload.get("serisesList"), list):
            return self._extract_series_items(payload["serisesList"], query_name)
        raise DanmakuSearchError("优酷弹幕搜索结果解析失败")

    def _expand_items_from_candidate_pages(self, items: list[DanmakuSearchItem]) -> list[DanmakuSearchItem]:
        if not items:
            return []
        expanded: list[DanmakuSearchItem] = []
        seen_groups: set[str] = set()
        for item in items:
            group_key = normalize_name(strip_episode_suffix(item.name)) or item.url
            if group_key in seen_groups:
                continue
            seen_groups.add(group_key)
            if len(seen_groups) > 3:
                break
            try:
                response = self._get(
                    item.url,
                    headers={"user-agent": self._SEARCH_USER_AGENT, "referer": "https://www.youku.com/"},
                    follow_redirects=True,
                    timeout=10.0,
                )
            except Exception:
                continue
            expanded.extend(self._extract_detail_episode_items(response.text))
        if not expanded:
            return items
        return self._merge_search_items(expanded, items)

    def _extract_page_component_items(self, items: list[dict]) -> list[DanmakuSearchItem]:
        results: list[DanmakuSearchItem] = []
        for item in items:
            common = item.get("commonData") or {}
            if not self._is_youku_common_data(common):
                continue
            component_results: list[DanmakuSearchItem] = []
            for episode in self._component_episode_items(item):
                component_results.append(episode)
            title = self._component_primary_title(common)
            url = self._component_primary_url(common)
            if title and url and not component_results:
                component_results.append(
                    DanmakuSearchItem(
                        provider=self.key,
                        name=title,
                        url=url,
                        duration_seconds=self._to_duration_seconds(common.get("duration")),
                    )
                )
            results.extend(component_results)
        return results

    def _extract_series_items(self, items: list[dict], query_name: str) -> list[DanmakuSearchItem]:
        results: list[DanmakuSearchItem] = []
        for item in items:
            title = str(item.get("title") or item.get("displayName") or "").strip()
            url = self._series_item_url(item)
            if title and url and self._series_title_matches_query(query_name, title):
                results.append(
                    DanmakuSearchItem(
                        provider=self.key,
                        name=title,
                        url=url,
                        duration_seconds=self._to_duration_seconds(item.get("duration")),
                    )
                )
        return results

    def _is_youku_common_data(self, common: dict) -> bool:
        if int(common.get("isYouku") or 0) == 1 or int(common.get("hasYouku") or 0) == 1:
            return True
        for candidate in (
            common.get("videoLink"),
            ((common.get("leftButtonDTO") or {}).get("action") or {}).get("value"),
            ((common.get("action") or {}).get("value")),
        ):
            if "youku.com" in str(candidate or ""):
                return True
        return False

    def _component_episode_items(self, item: dict) -> list[DanmakuSearchItem]:
        component_map = item.get("componentMap") or {}
        episodes = (component_map.get("1035") or {}).get("data") or []
        output: list[DanmakuSearchItem] = []
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            title = str(episode.get("title") or "").strip()
            url = self._component_episode_url(episode)
            if not title or not url:
                continue
            output.append(
                DanmakuSearchItem(
                    provider=self.key,
                    name=title,
                    url=url,
                    duration_seconds=self._to_duration_seconds(episode.get("duration")),
                )
            )
        return output

    def _component_primary_url(self, common: dict) -> str:
        for candidate in (
            common.get("videoLink"),
            ((common.get("leftButtonDTO") or {}).get("action") or {}).get("value"),
            ((common.get("action") or {}).get("value")),
        ):
            url = self._normalize_youku_url(str(candidate or "").strip())
            if url:
                return url
        return ""

    def _component_primary_title(self, common: dict) -> str:
        title = str((common.get("titleDTO") or {}).get("displayName") or "").strip()
        if not title or extract_episode_number(title) is not None:
            return title
        update_notice = str(common.get("updateNotice") or "").strip()
        episode = extract_episode_number(update_notice)
        if episode is None:
            return title
        return f"{title} 第{episode}集"

    def _extract_detail_episode_items(self, html_text: str) -> list[DanmakuSearchItem]:
        output: list[DanmakuSearchItem] = []
        for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]+aria-label="([^"]+)"', html_text, re.I):
            url = self._normalize_youku_url(html.unescape(match.group(1)))
            title = self._clean_detail_episode_title(html.unescape(match.group(2)).strip())
            if not url or not title:
                continue
            output.append(DanmakuSearchItem(provider=self.key, name=title, url=url))
        return output

    def _to_duration_seconds(self, value) -> int:
        try:
            return max(0, int(math.ceil(float(value or 0))))
        except (TypeError, ValueError):
            return 0

    def _clean_detail_episode_title(self, title: str) -> str:
        return re.sub(r"^(?:VIP|SVIP|预告|抢先看)\s+", "", title, flags=re.I).strip()

    def _merge_search_items(
        self, primary_items: list[DanmakuSearchItem], fallback_items: list[DanmakuSearchItem]
    ) -> list[DanmakuSearchItem]:
        merged: list[DanmakuSearchItem] = []
        seen: set[tuple[str, str]] = set()
        for item in [*primary_items, *fallback_items]:
            key = (item.name, item.url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _component_episode_url(self, episode: dict) -> str:
        video_id = str(episode.get("videoId") or "").strip()
        if video_id:
            return f"https://v.youku.com/v_show/id_{video_id}.html"
        action_value = str(((episode.get("action") or {}).get("value") or "")).strip()
        return self._normalize_youku_url(action_value)

    def _series_item_url(self, item: dict) -> str:
        video_id = str(item.get("videoId") or "").strip()
        if video_id:
            return f"https://v.youku.com/v_show/id_{video_id}.html"
        action_value = str(((item.get("action") or {}).get("value") or "")).strip()
        if not action_value:
            return ""
        parsed = urlparse(action_value)
        vid = parse_qs(parsed.query).get("vid", [""])[0].strip()
        if not vid:
            return ""
        return f"https://v.youku.com/v_show/id_{vid}.html"

    def _normalize_youku_url(self, value: str) -> str:
        candidate = str(value or "").strip()
        if not candidate:
            return ""
        if candidate.startswith("https://v.youku.com/v_show/"):
            return candidate
        if candidate.startswith("//"):
            candidate = f"https:{candidate}"
        parsed = urlparse(candidate)
        vid = parse_qs(parsed.query).get("vid", [""])[0].strip()
        if vid:
            return f"https://v.youku.com/v_show/id_{vid}.html"
        return ""

    def _series_title_matches_query(self, query_name: str, title: str) -> bool:
        normalized_query = normalize_name(query_name).casefold()
        normalized_title = normalize_name(title).casefold()
        if not normalized_query or not normalized_title:
            return False
        return normalized_query in normalized_title or normalized_title in normalized_query

    def _match_first(self, text: str, patterns: tuple[str, ...]) -> str:
        for pattern in patterns:
            match = re.search(pattern, text, re.S)
            if match is not None:
                return match.group(1)
        return ""
