from __future__ import annotations

import hashlib
import html
import json
import re
import time
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

import httpx

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem
from atv_player.danmaku.utils import normalize_name, similarity_score


_SEARCH_TYPE_PRIORITY = {"media_bangumi": 0, "media_ft": 1, "video": 2}
_RISK_CONTROL_CODES = {-352, -412}
_BROWSER_HEADERS = {
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "referer": "https://www.bilibili.com/",
    "origin": "https://www.bilibili.com",
}
_MIXIN_KEY_ENC_TAB = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]


class BilibiliDanmakuProvider:
    key = "bilibili"

    def __init__(self, get=httpx.get) -> None:
        self._get = get
        self._metadata_by_url: dict[str, DanmakuSearchItem] = {}

    def supports(self, page_url: str) -> bool:
        return "bilibili.com" in page_url or "b23.tv" in page_url

    def search(self, name: str) -> list[DanmakuSearchItem]:
        normalized = normalize_name(name)
        self._prime_web_state()
        items: list[DanmakuSearchItem] = []
        for search_type in ("media_bangumi", "media_ft", "video"):
            payload = self._search_payload(normalized, search_type)
            items.extend(self._parse_search_results(payload, normalized, search_type))
        items.sort(key=lambda item: (_SEARCH_TYPE_PRIORITY[item.search_type], -item.ratio, -item.simi))
        for item in items:
            self._metadata_by_url[item.url] = item
        return items

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        candidate = self._metadata_by_url.get(page_url) or DanmakuSearchItem(provider=self.key, name="", url=page_url)
        cid = self._resolve_cid(candidate)
        xml_text = self._get(
            f"https://comment.bilibili.com/{cid}.xml",
            headers={"user-agent": "Mozilla/5.0", "referer": page_url},
            timeout=10.0,
            follow_redirects=True,
        ).text
        return self._parse_xml_records(xml_text)

    def _search_payload(self, keyword: str, search_type: str) -> dict:
        params = {"keyword": keyword, "search_type": search_type}
        params.update(self._build_wbi_params(params))
        payload = self._request_search(params)
        if payload.get("code") in _RISK_CONTROL_CODES:
            self._refresh_ticket()
            retry_params = {"keyword": keyword, "search_type": search_type}
            retry_params.update(self._build_wbi_params(retry_params))
            payload = self._request_search(retry_params)
        if payload.get("code") != 0:
            raise DanmakuSearchError(f"Bilibili search failed: {payload.get('code')}")
        return payload

    def _request_search(self, params: dict[str, str]) -> dict:
        return self._request_json(
            "https://api.bilibili.com/x/web-interface/wbi/search/type",
            params=params,
            headers=_BROWSER_HEADERS,
            error_cls=DanmakuSearchError,
            context="search",
        )

    def _build_wbi_params(self, params: dict[str, str]) -> dict[str, str]:
        nav = self._request_json(
            "https://api.bilibili.com/x/web-interface/nav",
            headers=_BROWSER_HEADERS,
            error_cls=DanmakuSearchError,
            context="nav",
        )
        wbi_img = (nav.get("data") or {}).get("wbi_img") or {}
        img_key = str(wbi_img.get("img_url") or "").rsplit("/", 1)[-1].split(".", 1)[0]
        sub_key = str(wbi_img.get("sub_url") or "").rsplit("/", 1)[-1].split(".", 1)[0]
        mixin_source = img_key + sub_key
        mixin = "".join(mixin_source[index] for index in _MIXIN_KEY_ENC_TAB if index < len(mixin_source))[:32]
        signed = {key: str(value) for key, value in params.items()}
        signed["wts"] = str(int(time.time()))
        query = urlencode(sorted(signed.items()))
        signed["w_rid"] = hashlib.md5(f"{query}{mixin}".encode()).hexdigest()
        return signed

    def _refresh_ticket(self) -> None:
        self._get(
            "https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket",
            params={"key_id": "ec02", "hexsign": "ignored", "context[ts]": str(int(time.time()))},
            headers=_BROWSER_HEADERS,
            timeout=10.0,
            follow_redirects=True,
        )

    def _prime_web_state(self) -> None:
        self._request_json(
            "https://api.bilibili.com/x/frontend/finger/spi",
            headers=_BROWSER_HEADERS,
            error_cls=DanmakuSearchError,
            context="spi",
        )

    def _parse_search_results(self, payload: dict, query_name: str, search_type: str) -> list[DanmakuSearchItem]:
        output: list[DanmakuSearchItem] = []
        for raw in ((payload.get("data") or {}).get("result") or []):
            title = html.unescape(re.sub(r"<[^>]+>", "", str(raw.get("title") or ""))).strip()
            url = str(raw.get("url") or raw.get("arcurl") or "").strip()
            if url.startswith("//"):
                url = f"https:{url}"
            if not title or not url:
                continue
            ratio = similarity_score(query_name, title)
            output.append(
                DanmakuSearchItem(
                    provider=self.key,
                    name=title,
                    url=url,
                    ratio=ratio,
                    simi=ratio,
                    cid=self._to_int(raw.get("cid")),
                    bvid=str(raw.get("bvid") or ""),
                    aid=self._to_int(raw.get("aid")),
                    ep_id=self._to_int(raw.get("ep_id")),
                    season_id=self._to_int(raw.get("season_id")),
                    search_type=search_type,
                )
            )
        return output

    def _resolve_cid(self, candidate: DanmakuSearchItem) -> int:
        if candidate.cid is not None:
            return candidate.cid
        if candidate.ep_id is not None:
            cid = self._cid_from_season(ep_id=candidate.ep_id, season_id=candidate.season_id, title=candidate.name)
            if cid is not None:
                return cid
        if candidate.season_id is not None:
            cid = self._cid_from_season(ep_id=None, season_id=candidate.season_id, title=candidate.name)
            if cid is not None:
                return cid
        if candidate.bvid or candidate.aid is not None:
            cid = self._cid_from_pagelist(candidate)
            if cid is not None:
                return cid
        cid = self._cid_from_html(candidate.url)
        if cid is None:
            raise DanmakuResolveError(f"Bilibili page missing cid: {candidate.url}")
        return cid

    def _cid_from_season(self, ep_id: int | None, season_id: int | None, title: str) -> int | None:
        params = {"ep_id": ep_id} if ep_id is not None else {"season_id": season_id}
        payload = self._get(
            "https://api.bilibili.com/pgc/view/web/season",
            params=params,
            timeout=10.0,
            follow_redirects=True,
        ).json()
        episodes = ((payload.get("result") or {}).get("episodes") or [])
        if ep_id is not None:
            for episode in episodes:
                if self._to_int(episode.get("ep_id")) == ep_id:
                    cid = self._to_int(episode.get("cid"))
                    if cid is not None:
                        return cid
        target = normalize_name(title)
        if target:
            for episode in episodes:
                candidate_name = normalize_name(
                    str(episode.get("share_copy") or episode.get("long_title") or episode.get("title") or "")
                )
                if candidate_name == target:
                    cid = self._to_int(episode.get("cid"))
                    if cid is not None:
                        return cid
        if episodes:
            return self._to_int(episodes[0].get("cid"))
        return None

    def _cid_from_pagelist(self, candidate: DanmakuSearchItem) -> int | None:
        params = {"bvid": candidate.bvid} if candidate.bvid else {"aid": candidate.aid}
        payload = self._get(
            "https://api.bilibili.com/x/player/pagelist",
            params=params,
            timeout=10.0,
            follow_redirects=True,
        ).json()
        pages = payload.get("data") or []
        target = normalize_name(candidate.name)
        for page in pages:
            part = normalize_name(str(page.get("part") or ""))
            if part and target and (part == target or part in target or target in part):
                return self._to_int(page.get("cid"))
        if pages:
            return self._to_int(pages[0].get("cid"))
        return None

    def _cid_from_html(self, page_url: str) -> int | None:
        text = self._get(
            page_url,
            headers={"user-agent": "Mozilla/5.0"},
            timeout=10.0,
            follow_redirects=True,
        ).text
        state_match = re.search(r"__INITIAL_STATE__=(\{.*?\})</script>", text)
        if state_match:
            payload = json.loads(state_match.group(1))
            video_data = payload.get("videoData") or {}
            cid = self._to_int(video_data.get("cid"))
            if cid is not None:
                return cid
        match = re.search(r'"cid"\s*:\s*(\d+)', text)
        if match is not None:
            return int(match.group(1))
        return None

    def _parse_xml_records(self, xml_text: str) -> list[DanmakuRecord]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise DanmakuResolveError("Bilibili danmaku XML is invalid") from exc
        records: list[DanmakuRecord] = []
        for element in root.findall("d"):
            params = str(element.attrib.get("p") or "").split(",")
            if len(params) < 4:
                continue
            content = (element.text or "").strip()
            if not content:
                continue
            records.append(
                DanmakuRecord(
                    time_offset=float(params[0]),
                    pos=int(params[1]),
                    color=str(params[3]),
                    content=content,
                )
            )
        return records

    @staticmethod
    def _to_int(value: object) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _request_json(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        error_cls: type[Exception],
        context: str,
    ) -> dict:
        response = self._get(
            url,
            params=params,
            headers=headers,
            timeout=10.0,
            follow_redirects=True,
        )
        try:
            return response.json()
        except Exception as exc:
            status_code = getattr(response, "status_code", None)
            if status_code is not None:
                raise error_cls(f"Bilibili {context} request failed with HTTP {status_code}") from exc
            raise error_cls(f"Bilibili {context} returned a non-JSON response") from exc
