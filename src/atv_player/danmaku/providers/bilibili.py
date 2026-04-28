from __future__ import annotations

import hashlib
import html
import re
import time
from urllib.parse import urlencode

import httpx

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem
from atv_player.danmaku.utils import normalize_name, similarity_score


_SEARCH_TYPE_PRIORITY = {"media_bangumi": 0, "media_ft": 1, "video": 2}
_RISK_CONTROL_CODES = {-352, -412}
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
        items: list[DanmakuSearchItem] = []
        for search_type in ("media_bangumi", "media_ft", "video"):
            payload = self._search_payload(normalized, search_type)
            items.extend(self._parse_search_results(payload, normalized, search_type))
        items.sort(key=lambda item: (_SEARCH_TYPE_PRIORITY[item.search_type], -item.ratio, -item.simi))
        for item in items:
            self._metadata_by_url[item.url] = item
        return items

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        raise NotImplementedError("Bilibili danmaku resolution is not implemented yet")

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
        response = self._get(
            "https://api.bilibili.com/x/web-interface/wbi/search/type",
            params=params,
            headers={
                "user-agent": "Mozilla/5.0",
                "referer": "https://www.bilibili.com/",
                "origin": "https://www.bilibili.com",
            },
            timeout=10.0,
            follow_redirects=True,
        )
        return response.json()

    def _build_wbi_params(self, params: dict[str, str]) -> dict[str, str]:
        nav = self._get(
            "https://api.bilibili.com/x/web-interface/nav",
            timeout=10.0,
            follow_redirects=True,
        ).json()
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
            headers={"user-agent": "Mozilla/5.0", "referer": "https://www.bilibili.com/"},
            timeout=10.0,
            follow_redirects=True,
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

    @staticmethod
    def _to_int(value: object) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
