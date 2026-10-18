from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from math import isfinite
from urllib.parse import urlparse

import httpx

from atv_player.danmaku.errors import DanmakuResolveError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem
from atv_player.danmaku.providers._concurrency import iter_bounded_settled
from atv_player.danmaku.utils import extract_episode_number, should_filter_name

_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "atv-player/0.1.0",
}
_MAX_SUBJECTS = 5


def _default_nodes() -> tuple[str, ...]:
    utc_offset = datetime.now().astimezone().utcoffset()
    if utc_offset == timedelta(hours=8):
        return (
            "https://api.animeko.org",
            "https://danmaku-global.myani.org",
            "https://danmaku-cn.myani.org",
            "https://s1.animeko.openani.org",
        )
    return (
        "https://danmaku-global.myani.org",
        "https://api.animeko.org",
        "https://s1.animeko.openani.org",
        "https://danmaku-cn.myani.org",
    )


class AnimekoDanmakuProvider:
    key = "animeko"

    def __init__(
        self,
        get: Callable[..., httpx.Response] = httpx.get,
        post: Callable[..., httpx.Response] = httpx.post,
        nodes: Sequence[str] | None = None,
        search_nodes: Sequence[str] = (
            "https://api.bangumi.lol",
            "https://api.bgm.tv",
            # danmu_api f56b34f 迁移后的镜像域名,作为兜底节点
            "https://api.bangumi.vip",
        ),
    ) -> None:
        self._get = get
        self._post = post
        self._nodes = tuple(nodes or _default_nodes())
        self._search_nodes = tuple(search_nodes)
        self._subject_host = ""
        self._danmaku_host = ""

    def supports(self, page_url: str) -> bool:
        parsed = urlparse(page_url)
        return (
            parsed.scheme == self.key
            and parsed.netloc == "episode"
            and bool(parsed.path.strip("/"))
        )

    def search(
        self,
        name: str,
        original_name: str | None = None,
    ) -> list[DanmakuSearchItem]:
        requested_episode = extract_episode_number(original_name or name)
        subjects = self._search_subjects(name)
        items: list[DanmakuSearchItem] = []
        for batch in iter_bounded_settled(
            subjects[:_MAX_SUBJECTS],
            lambda subject: self._expand_subject(name, subject),
        ):
            for settled in batch:
                if settled.error is None and settled.value is not None:
                    items.extend(settled.value)
        if requested_episode is None:
            return items
        matched = [
            item
            for item in items
            if extract_episode_number(item.name) == requested_episode
        ]
        return matched if matched else items[:3]

    def _expand_subject(
        self,
        query_name: str,
        subject: object,
    ) -> list[DanmakuSearchItem]:
        if not isinstance(subject, dict):
            return []
        subject_id = str(subject.get("id") or "").strip()
        titles = [
            str(subject.get("name_cn") or "").strip(),
            str(subject.get("name") or "").strip(),
        ]
        title = next(
            (
                candidate
                for candidate in titles
                if candidate and not should_filter_name(query_name, candidate)
            ),
            "",
        )
        if not subject_id or not title:
            return []
        details = self._subject(subject_id)
        episodes = details.get("episodes") if isinstance(details, dict) else None
        if not isinstance(episodes, list):
            return []
        items: list[DanmakuSearchItem] = []
        for episode in episodes:
            if not isinstance(episode, dict) or episode.get("type") != "MAIN":
                continue
            episode_id = str(episode.get("episodeId") or "").strip()
            episode_no = str(episode.get("sort") or episode.get("ep") or "").strip()
            if not episode_id or not episode_no:
                continue
            episode_title = str(
                episode.get("nameCn") or episode.get("name") or ""
            ).strip()
            items.append(
                DanmakuSearchItem(
                    provider=self.key,
                    name=f"{title} 第{episode_no}集 {episode_title}".strip(),
                    url=f"animeko://episode/{episode_id}",
                    resolve_context={
                        "subject_id": subject_id,
                        "episode_id": episode_id,
                    },
                )
            )
        return items

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        episode_id = self._episode_id(page_url)
        failures: list[Exception] = []
        for host in self._ordered_nodes(self._danmaku_host):
            try:
                payload = self._get_json(f"{host}/v1/danmaku/{episode_id}")
            except (httpx.HTTPError, TypeError, ValueError) as exc:
                failures.append(exc)
                continue
            comments = payload.get("danmakuList") if isinstance(payload, dict) else None
            if not isinstance(comments, list):
                failures.append(ValueError("missing danmakuList"))
                continue
            self._danmaku_host = host
            return [
                record for row in comments if (record := self._record(row)) is not None
            ]
        error = DanmakuResolveError("Animeko弹幕获取失败")
        if failures:
            raise error from failures[-1]
        raise error

    def _search_subjects(self, keyword: str) -> list[object]:
        for host in self._search_nodes:
            try:
                response = self._post(
                    f"{host}/v0/search/subjects",
                    json={"keyword": keyword, "filter": {"type": [2]}},
                    headers=_HEADERS,
                    params={"limit": 20, "offset": 0},
                    timeout=5.0,
                    follow_redirects=True,
                )
                if response.status_code >= 400:
                    continue
                payload = response.json()
            except (httpx.HTTPError, TypeError, ValueError):
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, list):
                return data
        return []

    def _subject(self, subject_id: str) -> object:
        for host in self._ordered_nodes(self._subject_host):
            try:
                payload = self._get_json(f"{host}/v2/subjects/{subject_id}")
            except (httpx.HTTPError, TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("id"):
                self._subject_host = host
                return payload
        return {}

    def _get_json(self, url: str) -> object:
        response = self._get(
            url,
            headers=_HEADERS,
            timeout=3.0,
            follow_redirects=True,
        )
        if response.status_code >= 400:
            raise httpx.HTTPError(f"Animeko returned {response.status_code}")
        return response.json()

    def _ordered_nodes(self, preferred: str) -> tuple[str, ...]:
        if preferred not in self._nodes:
            return self._nodes
        return (preferred, *(node for node in self._nodes if node != preferred))

    def _episode_id(self, page_url: str) -> str:
        if not self.supports(page_url):
            raise DanmakuResolveError("Animeko弹幕地址无效")
        return urlparse(page_url).path.strip("/")

    def _record(self, row: object) -> DanmakuRecord | None:
        if not isinstance(row, dict):
            return None
        info = row.get("danmakuInfo")
        if not isinstance(info, dict):
            return None
        content = str(info.get("text") or "").strip()
        if not content:
            return None
        try:
            raw_time = float(str(info.get("playTime"))) / 1000.0
            color = int(str(info.get("color")))
        except (TypeError, ValueError):
            return None
        if not isfinite(raw_time):
            return None
        if color < 0 or color > 0xFFFFFF:
            color = 0xFFFFFF
        return DanmakuRecord(
            time_offset=max(0.0, raw_time),
            pos={"NORMAL": 1, "TOP": 5, "BOTTOM": 4}.get(
                str(info.get("location") or "").upper(),
                1,
            ),
            color=str(color),
            content=content,
        )
