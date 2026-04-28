from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem


class YoukuDanmakuProvider:
    key = "youku"

    def __init__(self, get=httpx.get) -> None:
        self._get = get

    def supports(self, page_url: str) -> bool:
        return "youku.com" in page_url

    def search(self, name: str) -> list[DanmakuSearchItem]:
        response = self._get(
            "https://search.youku.com/api/search",
            params={"appScene": "show_episode", "keyword": name, "appCaller": "h5"},
            headers={"user-agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=10.0,
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise DanmakuSearchError("优酷弹幕搜索结果解析失败") from exc
        return self._extract_search_items(payload)

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        response = self._get(page_url, headers={"user-agent": "Mozilla/5.0"}, follow_redirects=True, timeout=10.0)
        vid_match = re.search(r'"vid":"([^"]+)"', response.text)
        version_match = re.search(r'dataVersion="(\d+)"', response.text)
        if vid_match is None:
            raise DanmakuResolveError("优酷页面缺少 vid")
        vid = vid_match.group(1)
        data_version = version_match.group(1) if version_match else "0"
        query = urlencode(
            {"vid": vid, "mat": 0, "mcount": 1, "type": 1, "ct": 1001, "sver": 3, "dataVersion": data_version}
        )
        payload = self._get(
            f"https://acs.youku.com/h5/mopen.youku.danmu.list/1.0/?{query}",
            headers={"user-agent": "Mozilla/5.0", "referer": page_url},
            follow_redirects=True,
            timeout=10.0,
        ).json()
        items = ((payload.get("data") or {}).get("result") or [])
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

    def _extract_search_items(self, payload: dict) -> list[DanmakuSearchItem]:
        if isinstance(payload.get("pageComponentList"), list):
            return self._extract_page_component_items(payload["pageComponentList"])
        if isinstance(payload.get("serisesList"), list):
            return self._extract_series_items(payload["serisesList"])
        raise DanmakuSearchError("优酷弹幕搜索结果解析失败")

    def _extract_page_component_items(self, items: list[dict]) -> list[DanmakuSearchItem]:
        results: list[DanmakuSearchItem] = []
        for item in items:
            common = item.get("commonData") or {}
            title = str((common.get("titleDTO") or {}).get("displayName") or "").strip()
            url = str(common.get("videoLink") or "").strip()
            if title and url:
                results.append(DanmakuSearchItem(provider=self.key, name=title, url=url))
        return results

    def _extract_series_items(self, items: list[dict]) -> list[DanmakuSearchItem]:
        results: list[DanmakuSearchItem] = []
        for item in items:
            title = str(item.get("title") or item.get("displayName") or "").strip()
            url = self._series_item_url(item)
            if title and url:
                results.append(DanmakuSearchItem(provider=self.key, name=title, url=url))
        return results

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
