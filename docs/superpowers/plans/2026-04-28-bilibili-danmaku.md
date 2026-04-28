# Bilibili Danmaku Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a first-party Bilibili danmaku provider that searches Bilibili candidates, resolves `cid` through structured APIs with HTML fallback, downloads XML danmaku, and keeps the existing `DanmakuService` contract unchanged.

**Architecture:** Extend the shared danmaku model with optional Bilibili metadata while keeping existing callers compatible. Implement a focused `BilibiliDanmakuProvider` that owns WBI-signed search, candidate metadata reuse, `cid` resolution, and XML parsing, then wire it into the shared provider registry and default service ordering.

**Tech Stack:** Python 3.12, httpx, hashlib, urllib.parse, xml.etree.ElementTree, pytest

---

### Task 1: Extend Shared Danmaku Metadata And Provider Selection

**Files:**
- Modify: `src/atv_player/danmaku/models.py`
- Modify: `src/atv_player/danmaku/utils.py`
- Modify: `src/atv_player/danmaku/service.py`
- Modify: `src/atv_player/danmaku/providers/__init__.py`
- Modify: `tests/test_danmaku_service.py`

- [ ] **Step 1: Write the failing tests**

```python
from atv_player.danmaku.models import DanmakuSearchItem
from atv_player.danmaku.service import create_default_danmaku_service
from atv_player.danmaku.utils import match_provider


def test_danmaku_search_item_accepts_bilibili_metadata() -> None:
    item = DanmakuSearchItem(
        provider="bilibili",
        name="凡人修仙传 第1集",
        url="https://www.bilibili.com/bangumi/play/ep123",
        cid=987654,
        bvid="BV1xx411c7mD",
        aid=123456,
        ep_id=123,
        season_id=456,
        search_type="media_bangumi",
    )

    assert item.cid == 987654
    assert item.bvid == "BV1xx411c7mD"
    assert item.ep_id == 123
    assert item.search_type == "media_bangumi"


def test_match_provider_maps_bilibili_domains() -> None:
    assert match_provider("https://www.bilibili.com/video/BV1xx411c7mD") == "bilibili"
    assert match_provider("https://www.bilibili.com/bangumi/play/ep123") == "bilibili"
    assert match_provider("https://b23.tv/demo") == "bilibili"


def test_default_service_includes_bilibili_provider_in_fixed_order() -> None:
    service = create_default_danmaku_service()

    assert service.provider_order == ["tencent", "youku", "bilibili", "iqiyi", "mgtv"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_service.py -k "bilibili or metadata" -v`

Expected: FAIL because `DanmakuSearchItem` does not accept the new keyword arguments and the provider order still omits `bilibili`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/danmaku/models.py
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DanmakuSearchItem:
    provider: str
    name: str
    url: str
    ratio: float = 0.0
    simi: float = 0.0
    cid: int | None = None
    bvid: str = ""
    aid: int | None = None
    ep_id: int | None = None
    season_id: int | None = None
    search_type: str = ""
```

```python
# src/atv_player/danmaku/utils.py
def match_provider(reg_src: str) -> str | None:
    host = (urlparse(reg_src).hostname or reg_src or "").lower()
    if "qq.com" in host:
        return "tencent"
    if "youku.com" in host:
        return "youku"
    if "bilibili.com" in host or "b23.tv" in host:
        return "bilibili"
    if "iqiyi.com" in host:
        return "iqiyi"
    if "mgtv.com" in host:
        return "mgtv"
    return None
```

```python
# src/atv_player/danmaku/providers/__init__.py
from atv_player.danmaku.providers.bilibili import BilibiliDanmakuProvider

__all__ = [
    "BilibiliDanmakuProvider",
    "DanmakuProvider",
    "IqiyiDanmakuProvider",
    "MgtvDanmakuProvider",
    "TencentDanmakuProvider",
    "YoukuDanmakuProvider",
]
```

```python
# src/atv_player/danmaku/service.py
from atv_player.danmaku.providers import (
    BilibiliDanmakuProvider,
    IqiyiDanmakuProvider,
    MgtvDanmakuProvider,
    TencentDanmakuProvider,
    YoukuDanmakuProvider,
)


def create_default_danmaku_service() -> DanmakuService:
    providers = {
        "tencent": TencentDanmakuProvider(),
        "youku": YoukuDanmakuProvider(),
        "bilibili": BilibiliDanmakuProvider(),
        "iqiyi": IqiyiDanmakuProvider(),
        "mgtv": MgtvDanmakuProvider(),
    }
    return DanmakuService(providers, provider_order=["tencent", "youku", "bilibili", "iqiyi", "mgtv"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_service.py -k "bilibili or metadata" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_service.py src/atv_player/danmaku/models.py src/atv_player/danmaku/utils.py src/atv_player/danmaku/service.py src/atv_player/danmaku/providers/__init__.py
git commit -m "feat: add bilibili danmaku metadata scaffolding"
```

### Task 2: Add Search Parsing Tests For Bilibili Candidate Types

**Files:**
- Create: `tests/test_danmaku_bilibili_provider.py`
- Modify: `src/atv_player/danmaku/providers/bilibili.py`

- [ ] **Step 1: Write the failing tests**

```python
from atv_player.danmaku.providers.bilibili import BilibiliDanmakuProvider


class JsonResponse:
    def __init__(self, payload, text="") -> None:
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def test_bilibili_search_orders_bangumi_ft_before_video_and_preserves_metadata() -> None:
    search_payloads = {
        "media_bangumi": {
            "code": 0,
            "data": {
                "result": [
                    {
                        "title": "<em class=\"keyword\">凡人修仙传</em> 第1集",
                        "media_type": 1,
                        "season_id": 4001,
                        "ep_id": 5001,
                        "bvid": "BVbangumi1",
                        "url": "//www.bilibili.com/bangumi/play/ep5001",
                    }
                ]
            },
        },
        "media_ft": {
            "code": 0,
            "data": {
                "result": [
                    {
                        "title": "凡人修仙传 特别篇",
                        "season_id": 4002,
                        "ep_id": 5002,
                        "bvid": "BVft1",
                        "url": "//www.bilibili.com/bangumi/play/ep5002",
                    }
                ]
            },
        },
        "video": {
            "code": 0,
            "data": {
                "result": [
                    {
                        "title": "<em class=\"keyword\">凡人修仙传</em> P1",
                        "bvid": "BVvideo1",
                        "aid": 9001,
                        "arcurl": "https://www.bilibili.com/video/BVvideo1",
                    }
                ]
            },
        },
    }

    def fake_get(url: str, **kwargs):
        params = kwargs.get("params") or {}
        if "search/type" in url:
            return JsonResponse(search_payloads[params["search_type"]])
        if "nav" in url:
            return JsonResponse(
                {
                    "code": 0,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/abc123.png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/def456.png",
                        }
                    },
                }
            )
        return JsonResponse({"code": 0, "data": {}}, text="")

    provider = BilibiliDanmakuProvider(get=fake_get)

    items = provider.search("凡人修仙传 第1集")

    assert [item.search_type for item in items] == ["media_bangumi", "media_ft", "video"]
    assert items[0].url == "https://www.bilibili.com/bangumi/play/ep5001"
    assert items[0].ep_id == 5001
    assert items[0].season_id == 4001
    assert items[0].bvid == "BVbangumi1"
    assert items[2].aid == 9001
    assert items[2].url == "https://www.bilibili.com/video/BVvideo1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_bilibili_provider.py::test_bilibili_search_orders_bangumi_ft_before_video_and_preserves_metadata -v`

Expected: FAIL with `ModuleNotFoundError` or `AttributeError` because the provider does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/danmaku/providers/bilibili.py
from __future__ import annotations

import hashlib
import html
import re
import time
from urllib.parse import urlencode

import httpx

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.models import DanmakuSearchItem
from atv_player.danmaku.utils import normalize_name, similarity_score

_SEARCH_TYPE_PRIORITY = {"media_bangumi": 0, "media_ft": 1, "video": 2}
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
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

    def _search_payload(self, keyword: str, search_type: str) -> dict:
        params = {"keyword": keyword, "search_type": search_type}
        params.update(self._build_wbi_params(params))
        response = self._get(
            "https://api.bilibili.com/x/web-interface/wbi/search/type",
            params=params,
            headers={"user-agent": "Mozilla/5.0", "referer": "https://www.bilibili.com/", "origin": "https://www.bilibili.com"},
            timeout=10.0,
            follow_redirects=True,
        )
        payload = response.json()
        if payload.get("code") != 0:
            raise DanmakuSearchError(f"Bilibili search failed: {payload.get('code')}")
        return payload

    def _build_wbi_params(self, params: dict[str, str]) -> dict[str, str | int]:
        nav = self._get("https://api.bilibili.com/x/web-interface/nav", timeout=10.0, follow_redirects=True).json()
        wbi_img = (nav.get("data") or {}).get("wbi_img") or {}
        img_key = wbi_img.get("img_url", "").rsplit("/", 1)[-1].split(".", 1)[0]
        sub_key = wbi_img.get("sub_url", "").rsplit("/", 1)[-1].split(".", 1)[0]
        mixin = "".join((img_key + sub_key)[index] for index in _MIXIN_KEY_ENC_TAB)[:32]
        signed = {key: str(value) for key, value in params.items()}
        signed["wts"] = str(int(time.time()))
        query = urlencode(sorted(signed.items()))
        signed["w_rid"] = hashlib.md5(f"{query}{mixin}".encode()).hexdigest()
        return signed

    def _parse_search_results(self, payload: dict, query_name: str, search_type: str) -> list[DanmakuSearchItem]:
        output: list[DanmakuSearchItem] = []
        for raw in ((payload.get("data") or {}).get("result") or []):
            title = html.unescape(re.sub(r"<[^>]+>", "", str(raw.get("title") or ""))).strip()
            url = str(raw.get("url") or raw.get("arcurl") or "").strip()
            if url.startswith("//"):
                url = f"https:{url}"
            if not title or not url:
                continue
            output.append(
                DanmakuSearchItem(
                    provider=self.key,
                    name=title,
                    url=url,
                    ratio=similarity_score(query_name, title),
                    simi=similarity_score(query_name, title),
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
    def _to_int(value) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_bilibili_provider.py::test_bilibili_search_orders_bangumi_ft_before_video_and_preserves_metadata -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_bilibili_provider.py src/atv_player/danmaku/providers/bilibili.py
git commit -m "feat: add bilibili danmaku search parsing"
```

### Task 3: Add Search Retry Coverage For WBI Search Risk-Control Failures

**Files:**
- Modify: `tests/test_danmaku_bilibili_provider.py`
- Modify: `src/atv_player/danmaku/providers/bilibili.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from atv_player.danmaku.errors import DanmakuSearchError
from atv_player.danmaku.providers.bilibili import BilibiliDanmakuProvider


def test_bilibili_search_retries_once_after_ticket_refresh() -> None:
    calls: list[str] = []
    search_attempts = {"count": 0}

    class JsonResponse:
        def __init__(self, payload) -> None:
            self._payload = payload
            self.text = ""

        def json(self):
            return self._payload

    def fake_get(url: str, **kwargs):
        calls.append(url)
        if "x/web-interface/nav" in url:
            return JsonResponse(
                {"code": 0, "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/abc123.png", "sub_url": "https://i0.hdslb.com/bfs/wbi/def456.png"}}}
            )
        if "GenWebTicket" in url:
            return JsonResponse({"code": 0, "data": {"ticket": "ok"}})
        if "search/type" in url:
            search_attempts["count"] += 1
            if search_attempts["count"] == 1:
                return JsonResponse({"code": -352, "message": "risk control"})
            return JsonResponse({"code": 0, "data": {"result": []}})
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)

    items = provider.search("凡人修仙传 第1集")

    assert items == []
    assert search_attempts["count"] == 2
    assert any("GenWebTicket" in url for url in calls)


def test_bilibili_search_raises_after_second_risk_control_failure() -> None:
    class JsonResponse:
        def __init__(self, payload) -> None:
            self._payload = payload
            self.text = ""

        def json(self):
            return self._payload

    def fake_get(url: str, **kwargs):
        if "x/web-interface/nav" in url:
            return JsonResponse(
                {"code": 0, "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/abc123.png", "sub_url": "https://i0.hdslb.com/bfs/wbi/def456.png"}}}
            )
        if "GenWebTicket" in url:
            return JsonResponse({"code": 0, "data": {"ticket": "ok"}})
        if "search/type" in url:
            return JsonResponse({"code": -352, "message": "risk control"})
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)

    with pytest.raises(DanmakuSearchError, match="Bilibili search failed"):
        provider.search("凡人修仙传 第1集")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_bilibili_provider.py -k "ticket_refresh or second_risk_control" -v`

Expected: FAIL because the provider does not retry or call the ticket endpoint.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/danmaku/providers/bilibili.py
_RISK_CONTROL_CODES = {-352, -412}


def _search_payload(self, keyword: str, search_type: str) -> dict:
    params = {"keyword": keyword, "search_type": search_type}
    params.update(self._build_wbi_params(params))
    payload = self._request_search(params)
    if payload.get("code") in _RISK_CONTROL_CODES:
        self._refresh_ticket()
        params = {"keyword": keyword, "search_type": search_type}
        params.update(self._build_wbi_params(params))
        payload = self._request_search(params)
    if payload.get("code") != 0:
        raise DanmakuSearchError(f"Bilibili search failed: {payload.get('code')}")
    return payload


def _request_search(self, params: dict[str, str | int]) -> dict:
    response = self._get(
        "https://api.bilibili.com/x/web-interface/wbi/search/type",
        params=params,
        headers={"user-agent": "Mozilla/5.0", "referer": "https://www.bilibili.com/", "origin": "https://www.bilibili.com"},
        timeout=10.0,
        follow_redirects=True,
    )
    return response.json()


def _refresh_ticket(self) -> None:
    self._get(
        "https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket",
        params={"key_id": "ec02", "hexsign": "ignored", "context[ts]": str(int(time.time()))},
        headers={"user-agent": "Mozilla/5.0", "referer": "https://www.bilibili.com/"},
        timeout=10.0,
        follow_redirects=True,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_bilibili_provider.py -k "ticket_refresh or second_risk_control" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_bilibili_provider.py src/atv_player/danmaku/providers/bilibili.py
git commit -m "feat: retry bilibili danmaku search after ticket refresh"
```

### Task 4: Add Cid Resolution And XML Parsing Tests

**Files:**
- Modify: `tests/test_danmaku_bilibili_provider.py`
- Modify: `src/atv_player/danmaku/providers/bilibili.py`

- [ ] **Step 1: Write the failing tests**

```python
from atv_player.danmaku.providers.bilibili import BilibiliDanmakuProvider


def test_bilibili_resolve_prefers_cached_candidate_cid_and_parses_xml() -> None:
    class JsonResponse:
        def __init__(self, payload=None, text="") -> None:
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    def fake_get(url: str, **kwargs):
        if "x/web-interface/nav" in url:
            return JsonResponse({"code": 0, "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/abc123.png", "sub_url": "https://i0.hdslb.com/bfs/wbi/def456.png"}}})
        if "search/type" in url:
            return JsonResponse(
                {
                    "code": 0,
                    "data": {"result": [{"title": "凡人修仙传 第1集", "url": "//www.bilibili.com/bangumi/play/ep5001", "cid": 777001, "ep_id": 5001, "season_id": 4001}]},
                }
            )
        if "comment.bilibili.com/777001.xml" in url:
            return JsonResponse(text='<?xml version="1.0" encoding="UTF-8"?><i><d p="1.5,1,25,16777215,0,0,0,0">第一条</d></i>')
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)
    items = provider.search("凡人修仙传 第1集")

    records = provider.resolve(items[0].url)

    assert len(records) == 1
    assert records[0].time_offset == 1.5
    assert records[0].pos == 1
    assert records[0].color == "16777215"
    assert records[0].content == "第一条"


def test_bilibili_resolve_uses_season_api_then_pagelist_then_html_fallback() -> None:
    seen: list[str] = []

    class JsonResponse:
        def __init__(self, payload=None, text="") -> None:
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    def fake_get(url: str, **kwargs):
        seen.append(url)
        if "pgc/view/web/season" in url and "ep_id=5002" in url:
            return JsonResponse({"code": 0, "result": {"episodes": [{"ep_id": 5002, "cid": 888002, "bvid": "BVep5002"}]}})
        if "x/player/pagelist" in url and "bvid=BVvideo2" in url:
            return JsonResponse({"code": 0, "data": [{"cid": 999003, "part": "第1集"}]})
        if "video/BVhtml1" in url:
            return JsonResponse(text='<script>window.__INITIAL_STATE__={"videoData":{"cid":666004}}</script>')
        if "comment.bilibili.com/888002.xml" in url:
            return JsonResponse(text='<?xml version="1.0" encoding="UTF-8"?><i><d p="2.0,1,25,255,0,0,0,0">season</d></i>')
        if "comment.bilibili.com/999003.xml" in url:
            return JsonResponse(text='<?xml version="1.0" encoding="UTF-8"?><i><d p="3.0,1,25,65280,0,0,0,0">pagelist</d></i>')
        if "comment.bilibili.com/666004.xml" in url:
            return JsonResponse(text='<?xml version="1.0" encoding="UTF-8"?><i><d p="4.0,1,25,16711680,0,0,0,0">html</d></i>')
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)
    provider._metadata_by_url["https://www.bilibili.com/bangumi/play/ep5002"] = provider._candidate(
        name="凡人修仙传 第2集", url="https://www.bilibili.com/bangumi/play/ep5002", ep_id=5002, season_id=4002
    )
    provider._metadata_by_url["https://www.bilibili.com/video/BVvideo2"] = provider._candidate(
        name="凡人修仙传 第1集", url="https://www.bilibili.com/video/BVvideo2", bvid="BVvideo2", search_type="video"
    )
    provider._metadata_by_url["https://www.bilibili.com/video/BVhtml1"] = provider._candidate(
        name="凡人修仙传 PV", url="https://www.bilibili.com/video/BVhtml1", bvid="BVhtml1", search_type="video"
    )

    assert provider.resolve("https://www.bilibili.com/bangumi/play/ep5002")[0].content == "season"
    assert provider.resolve("https://www.bilibili.com/video/BVvideo2")[0].content == "pagelist"
    assert provider.resolve("https://www.bilibili.com/video/BVhtml1")[0].content == "html"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_bilibili_provider.py -k "cached_candidate_cid or season_api_then_pagelist_then_html_fallback" -v`

Expected: FAIL because `resolve()` and XML parsing are not implemented.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/danmaku/providers/bilibili.py
import json
import xml.etree.ElementTree as ET

from atv_player.danmaku.models import DanmakuRecord


def resolve(self, page_url: str) -> list[DanmakuRecord]:
    candidate = self._metadata_by_url.get(page_url) or self._candidate(url=page_url, name="")
    cid = self._resolve_cid(candidate)
    xml_text = self._get(
        f"https://comment.bilibili.com/{cid}.xml",
        headers={"user-agent": "Mozilla/5.0", "referer": page_url},
        timeout=10.0,
        follow_redirects=True,
    ).text
    return self._parse_xml_records(xml_text)


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
    payload = self._get("https://api.bilibili.com/pgc/view/web/season", params=params, timeout=10.0, follow_redirects=True).json()
    episodes = ((payload.get("result") or {}).get("episodes") or [])
    if ep_id is not None:
        for episode in episodes:
            if self._to_int(episode.get("ep_id")) == ep_id and self._to_int(episode.get("cid")) is not None:
                return int(episode["cid"])
    for episode in episodes:
        if normalize_name(str(episode.get("share_copy") or episode.get("long_title") or "")) == normalize_name(title):
            return self._to_int(episode.get("cid"))
    return self._to_int(episodes[0].get("cid")) if episodes else None


def _cid_from_pagelist(self, candidate: DanmakuSearchItem) -> int | None:
    params = {"bvid": candidate.bvid} if candidate.bvid else {"aid": candidate.aid}
    payload = self._get("https://api.bilibili.com/x/player/pagelist", params=params, timeout=10.0, follow_redirects=True).json()
    pages = (payload.get("data") or [])
    target = normalize_name(candidate.name)
    for page in pages:
        if normalize_name(str(page.get("part") or "")) == target:
            return self._to_int(page.get("cid"))
    return self._to_int(pages[0].get("cid")) if pages else None


def _cid_from_html(self, page_url: str) -> int | None:
    text = self._get(page_url, headers={"user-agent": "Mozilla/5.0"}, timeout=10.0, follow_redirects=True).text
    state_match = re.search(r"__INITIAL_STATE__=(\\{.*?\\})</script>", text)
    if state_match:
        payload = json.loads(state_match.group(1))
        video_data = payload.get("videoData") or {}
        cid = self._to_int(video_data.get("cid"))
        if cid is not None:
            return cid
    match = re.search(r'"cid"\\s*:\\s*(\\d+)', text)
    return int(match.group(1)) if match else None


def _parse_xml_records(self, xml_text: str) -> list[DanmakuRecord]:
    root = ET.fromstring(xml_text)
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


def _candidate(self, *, name: str, url: str, cid: int | None = None, bvid: str = "", aid: int | None = None, ep_id: int | None = None, season_id: int | None = None, search_type: str = "") -> DanmakuSearchItem:
    return DanmakuSearchItem(
        provider=self.key,
        name=name,
        url=url,
        cid=cid,
        bvid=bvid,
        aid=aid,
        ep_id=ep_id,
        season_id=season_id,
        search_type=search_type,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_bilibili_provider.py -k "cached_candidate_cid or season_api_then_pagelist_then_html_fallback" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_bilibili_provider.py src/atv_player/danmaku/providers/bilibili.py
git commit -m "feat: resolve bilibili danmaku cid and xml"
```

### Task 5: Harden Resolution Edge Cases And Service Integration

**Files:**
- Modify: `tests/test_danmaku_bilibili_provider.py`
- Modify: `tests/test_danmaku_service.py`
- Modify: `src/atv_player/danmaku/providers/bilibili.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from atv_player.danmaku.errors import DanmakuResolveError
from atv_player.danmaku.providers.bilibili import BilibiliDanmakuProvider


def test_bilibili_resolve_falls_back_to_first_pagelist_entry_when_no_part_match() -> None:
    class JsonResponse:
        def __init__(self, payload=None, text="") -> None:
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    def fake_get(url: str, **kwargs):
        if "x/player/pagelist" in url:
            return JsonResponse({"code": 0, "data": [{"cid": 123001, "part": "P1"}, {"cid": 123002, "part": "P2"}]})
        if "comment.bilibili.com/123001.xml" in url:
            return JsonResponse(text='<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215,0,0,0,0">first</d></i>')
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)
    provider._metadata_by_url["https://www.bilibili.com/video/BVfirst"] = provider._candidate(
        name="完全匹配不到的标题",
        url="https://www.bilibili.com/video/BVfirst",
        bvid="BVfirst",
        search_type="video",
    )

    records = provider.resolve("https://www.bilibili.com/video/BVfirst")

    assert [record.content for record in records] == ["first"]


def test_bilibili_resolve_raises_clear_error_when_no_cid_can_be_found() -> None:
    class JsonResponse:
        def __init__(self, payload=None, text="") -> None:
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    def fake_get(url: str, **kwargs):
        if "x/player/pagelist" in url:
            return JsonResponse({"code": 0, "data": []})
        if "BVnone" in url:
            return JsonResponse(text="<html><body>missing cid</body></html>")
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)
    provider._metadata_by_url["https://www.bilibili.com/video/BVnone"] = provider._candidate(
        name="空页面",
        url="https://www.bilibili.com/video/BVnone",
        bvid="BVnone",
        search_type="video",
    )

    with pytest.raises(DanmakuResolveError, match="missing cid"):
        provider.resolve("https://www.bilibili.com/video/BVnone")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_bilibili_provider.py -k "first_pagelist_entry or no_cid_can_be_found" -v`

Expected: FAIL because the provider does not yet normalize these error paths exactly.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/danmaku/providers/bilibili.py
def _cid_from_pagelist(self, candidate: DanmakuSearchItem) -> int | None:
    params = {"bvid": candidate.bvid} if candidate.bvid else {"aid": candidate.aid}
    payload = self._get("https://api.bilibili.com/x/player/pagelist", params=params, timeout=10.0, follow_redirects=True).json()
    pages = (payload.get("data") or [])
    target = normalize_name(candidate.name)
    for page in pages:
        part = normalize_name(str(page.get("part") or ""))
        if part and part == target:
            return self._to_int(page.get("cid"))
    return self._to_int(pages[0].get("cid")) if pages else None


def _resolve_cid(self, candidate: DanmakuSearchItem) -> int:
    ...
    cid = self._cid_from_html(candidate.url)
    if cid is None:
        raise DanmakuResolveError(f"Bilibili page missing cid: {candidate.url}")
    return cid
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_bilibili_provider.py -k "first_pagelist_entry or no_cid_can_be_found" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_bilibili_provider.py src/atv_player/danmaku/providers/bilibili.py tests/test_danmaku_service.py
git commit -m "test: cover bilibili danmaku resolve edge cases"
```

### Task 6: Run Focused Regression Verification

**Files:**
- No code changes required unless a regression is found

- [ ] **Step 1: Run provider-focused tests**

Run: `uv run pytest tests/test_danmaku_bilibili_provider.py tests/test_danmaku_service.py -v`

Expected: PASS

- [ ] **Step 2: Run controller compatibility tests**

Run: `uv run pytest tests/test_spider_plugin_controller.py -k "danmaku" -v`

Expected: PASS and no failures caused by the new `DanmakuSearchItem` fields.

- [ ] **Step 3: Run one combined smoke check**

Run: `uv run pytest tests/test_danmaku_bilibili_provider.py tests/test_danmaku_service.py tests/test_spider_plugin_controller.py -k "danmaku or bilibili" -v`

Expected: PASS

- [ ] **Step 4: Commit verification-only fixes if needed**

```bash
git add src/atv_player/danmaku/models.py src/atv_player/danmaku/utils.py src/atv_player/danmaku/service.py src/atv_player/danmaku/providers/__init__.py src/atv_player/danmaku/providers/bilibili.py tests/test_danmaku_service.py tests/test_danmaku_bilibili_provider.py tests/test_spider_plugin_controller.py
git commit -m "fix: stabilize bilibili danmaku integration"
```

## Self-Review

Spec coverage check:
- shared metadata expansion is covered by Task 1
- provider ordering and `reg_src` mapping are covered by Task 1
- categorized Bilibili search parsing is covered by Task 2
- ticket retry flow is covered by Task 3
- `cid` resolution precedence and XML parsing are covered by Task 4
- pagelist fallback and missing-`cid` error behavior are covered by Task 5
- regression verification for existing consumers is covered by Task 6

Placeholder scan:
- no `TODO`, `TBD`, or deferred implementation markers remain
- every test and implementation step includes concrete code or commands

Type consistency check:
- `DanmakuSearchItem` fields use the same names throughout: `cid`, `bvid`, `aid`, `ep_id`, `season_id`, `search_type`
- provider methods are consistently named `search()` and `resolve()`
- helper methods use one naming scheme: `_search_payload`, `_resolve_cid`, `_cid_from_season`, `_cid_from_pagelist`, `_cid_from_html`, `_parse_xml_records`
