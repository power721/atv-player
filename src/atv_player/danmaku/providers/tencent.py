from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem


class TencentDanmakuProvider:
    key = "tencent"

    def __init__(self, get=httpx.get) -> None:
        self._get = get

    def supports(self, page_url: str) -> bool:
        return "qq.com" in page_url

    def search(self, name: str) -> list[DanmakuSearchItem]:
        response = self._get(
            "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch",
            params={"q": name, "query": name, "vversion_platform": "2"},
            headers={"user-agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=10.0,
        )
        try:
            payload = response.json()
            items = payload["data"]["normalList"]["itemList"]
        except Exception as exc:
            raise DanmakuSearchError("腾讯弹幕搜索结果解析失败") from exc
        results: list[DanmakuSearchItem] = []
        for item in items:
            video_info = item.get("videoInfo") or {}
            title = str(video_info.get("title") or "").strip()
            url = str(video_info.get("url") or "").strip()
            if title and url:
                results.append(DanmakuSearchItem(provider=self.key, name=title, url=url))
        return results

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        response = self._get(page_url, headers={"user-agent": "Mozilla/5.0"}, follow_redirects=True, timeout=10.0)
        match = re.search(r'"videoId":"([^"]+)"', response.text)
        if match is None:
            raise DanmakuResolveError("腾讯页面缺少 videoId")
        video_id = match.group(1)
        records: list[DanmakuRecord] = []
        seen: set[tuple[float, str]] = set()
        for segment in range(200):
            segment_response = self._get(
                f"https://dm.video.qq.com/barrage/segment/{quote(video_id)}/t/v1/{segment}",
                headers={"user-agent": "Mozilla/5.0"},
                follow_redirects=True,
                timeout=10.0,
            )
            payload = segment_response.json()
            barrage_list = payload.get("barrage_list") or []
            if not barrage_list:
                break
            for item in barrage_list:
                content = str(item.get("content") or "").strip()
                time_offset = float(item.get("time_offset") or 0.0)
                style = item.get("content_style") or {}
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
