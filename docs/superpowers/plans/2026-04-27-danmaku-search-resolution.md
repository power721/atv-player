# Danmaku Search And Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an application-internal danmaku service that searches Tencent and Youku candidate pages from a title, resolves a selected page into unified danmaku records, and returns XML output without adding HTTP routes or player integration.

**Architecture:** Create a dedicated `atv_player.danmaku` package with shared models, errors, utils, and service orchestration, then isolate provider-specific network parsing under `providers/`. Keep all behavior test-driven with mock HTTP responses and explicit not-implemented skeletons for iQIYI and MGTV.

**Tech Stack:** Python 3.12, httpx, lxml/stdlib XML escaping, pytest

---

### Task 1: Add Danmaku Package Scaffolding, Models, Errors, And Utility Functions

**Files:**
- Create: `src/atv_player/danmaku/__init__.py`
- Create: `src/atv_player/danmaku/models.py`
- Create: `src/atv_player/danmaku/errors.py`
- Create: `src/atv_player/danmaku/utils.py`
- Test: `tests/test_danmaku_utils.py`

- [ ] **Step 1: Write the failing tests**

```python
from atv_player.danmaku.models import DanmakuRecord
from atv_player.danmaku.utils import (
    build_xml,
    match_provider,
    normalize_name,
    should_filter_name,
)


def test_normalize_name_strips_noise_tokens() -> None:
    assert normalize_name(" 剑来 第1集【高清】(qq.com) ") == "剑来 第1集"


def test_match_provider_maps_known_domains() -> None:
    assert match_provider("https://v.qq.com/x/cover/demo.html") == "tencent"
    assert match_provider("https://v.youku.com/v_show/id_demo.html") == "youku"
    assert match_provider("https://www.iqiyi.com/v_demo.html") == "iqiyi"
    assert match_provider("https://www.mgtv.com/b/demo.html") == "mgtv"
    assert match_provider("https://example.com/watch/1") is None


def test_should_filter_name_rejects_unrelated_titles() -> None:
    target = normalize_name("剑来 第1集")
    assert should_filter_name(target, "凡人修仙传 第1集") is True
    assert should_filter_name(target, "剑来 第1集") is False


def test_build_xml_escapes_content_and_keeps_expected_shape() -> None:
    xml = build_xml(
        [
            DanmakuRecord(time_offset=1.5, pos=1, color="16777215", content="a < b & c"),
            DanmakuRecord(time_offset=3.0, pos=4, color="255", content='"quoted"'),
        ]
    )

    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?><i>')
    assert '<d p="1.5,1,25,16777215">a &lt; b &amp; c</d>' in xml
    assert '<d p="3.0,4,25,255">"quoted"</d>' in xml
    assert xml.endswith("</i>")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_utils.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'atv_player.danmaku'`

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


@dataclass(frozen=True, slots=True)
class DanmakuRecord:
    time_offset: float
    pos: int
    color: str
    content: str
```

```python
# src/atv_player/danmaku/errors.py
class DanmakuError(Exception):
    pass


class ProviderNotSupportedError(DanmakuError):
    pass


class DanmakuSearchError(DanmakuError):
    pass


class DanmakuResolveError(DanmakuError):
    pass


class DanmakuEmptyResultError(DanmakuError):
    pass
```

```python
# src/atv_player/danmaku/utils.py
from __future__ import annotations

import re
from difflib import SequenceMatcher
from html import escape
from typing import Sequence
from urllib.parse import urlparse

from atv_player.danmaku.models import DanmakuRecord

_NOISE_PATTERNS = (
    r"【[^】]*】",
    r"\[[^\]]*\]",
    r"\([^)]*(高清|超清|蓝光|qq\.com|youku\.com)[^)]*\)",
)


def normalize_name(name: str) -> str:
    value = str(name).strip()
    for pattern in _NOISE_PATTERNS:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def match_provider(reg_src: str) -> str | None:
    host = (urlparse(reg_src).hostname or reg_src or "").lower()
    if "qq.com" in host:
        return "tencent"
    if "youku.com" in host:
        return "youku"
    if "iqiyi.com" in host:
        return "iqiyi"
    if "mgtv.com" in host:
        return "mgtv"
    return None


def _simplify_name(name: str) -> str:
    value = normalize_name(name).casefold()
    value = re.sub(r"第\s*\d+\s*[集话期]", "", value)
    value = re.sub(r"[\W_]+", "", value)
    return value


def similarity_score(left: str, right: str) -> float:
    return SequenceMatcher(None, _simplify_name(left), _simplify_name(right)).ratio()


def should_filter_name(target: str, candidate: str) -> bool:
    left = _simplify_name(target)
    right = _simplify_name(candidate)
    if not left or not right:
        return False
    if left in right or right in left:
        return False
    return similarity_score(left, right) < 0.55


def build_xml(records: Sequence[DanmakuRecord]) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?><i>']
    for record in records:
        parts.append(
            f'<d p="{record.time_offset},{record.pos},25,{record.color}">{escape(record.content, quote=False)}</d>'
        )
    parts.append("</i>")
    return "".join(parts)
```

```python
# src/atv_player/danmaku/__init__.py
from atv_player.danmaku.errors import (
    DanmakuEmptyResultError,
    DanmakuError,
    DanmakuResolveError,
    DanmakuSearchError,
    ProviderNotSupportedError,
)
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem
from atv_player.danmaku.utils import build_xml, match_provider, normalize_name, should_filter_name

__all__ = [
    "DanmakuEmptyResultError",
    "DanmakuError",
    "DanmakuRecord",
    "DanmakuResolveError",
    "DanmakuSearchError",
    "DanmakuSearchItem",
    "ProviderNotSupportedError",
    "build_xml",
    "match_provider",
    "normalize_name",
    "should_filter_name",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_utils.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_utils.py src/atv_player/danmaku/__init__.py src/atv_player/danmaku/models.py src/atv_player/danmaku/errors.py src/atv_player/danmaku/utils.py
git commit -m "feat: add danmaku utility layer"
```

### Task 2: Add Service-Orchestration And Provider Dispatch

**Files:**
- Create: `src/atv_player/danmaku/service.py`
- Create: `src/atv_player/danmaku/providers/__init__.py`
- Create: `src/atv_player/danmaku/providers/base.py`
- Test: `tests/test_danmaku_service.py`
- Modify: `src/atv_player/danmaku/__init__.py`

- [ ] **Step 1: Write the failing tests**

```python
from atv_player.danmaku.errors import ProviderNotSupportedError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem
from atv_player.danmaku.service import DanmakuService


class FakeProvider:
    def __init__(self, key: str, items: list[DanmakuSearchItem], records: list[DanmakuRecord]) -> None:
        self.key = key
        self.items = items
        self.records = records
        self.search_calls: list[str] = []
        self.resolve_calls: list[str] = []

    def search(self, name: str) -> list[DanmakuSearchItem]:
        self.search_calls.append(name)
        return list(self.items)

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        self.resolve_calls.append(page_url)
        return list(self.records)

    def supports(self, page_url: str) -> bool:
        return self.key in page_url


def test_search_danmu_prefers_provider_from_reg_src() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第1集", url="https://tencent/item", ratio=0.9, simi=0.8)],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="剑来 第1集", url="https://youku/item", ratio=0.8, simi=0.8)],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    results = service.search_danmu("剑来 第1集", "https://v.qq.com/x/cover/demo.html")

    assert [item.provider for item in results] == ["tencent"]
    assert tencent.search_calls == ["剑来 第1集"]
    assert youku.search_calls == []


def test_search_danmu_aggregates_and_sorts_results_without_reg_src() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第1集", url="https://tencent/item", ratio=0.82, simi=0.82)],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="剑来 第1集", url="https://youku/item", ratio=0.91, simi=0.91)],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    results = service.search_danmu("剑来 第1集")

    assert [item.provider for item in results] == ["youku", "tencent"]


def test_search_danmu_falls_back_to_default_order_for_unknown_reg_src() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第1集", url="https://tencent/item", ratio=0.82, simi=0.82)],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="剑来 第1集", url="https://youku/item", ratio=0.91, simi=0.91)],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    results = service.search_danmu("剑来 第1集", "https://unknown.example/video/1")

    assert [item.provider for item in results] == ["youku", "tencent"]


def test_resolve_danmu_dispatches_by_url_and_builds_xml() -> None:
    tencent = FakeProvider(
        "tencent",
        [],
        [DanmakuRecord(time_offset=1.0, pos=1, color="16777215", content="hello")],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    xml = service.resolve_danmu("https://video.tencent/item")

    assert '<d p="1.0,1,25,16777215">hello</d>' in xml
    assert tencent.resolve_calls == ["https://video.tencent/item"]


def test_resolve_danmu_raises_for_unknown_provider_url() -> None:
    service = DanmakuService({}, provider_order=[])

    try:
        service.resolve_danmu("https://unknown.example/video/1")
    except ProviderNotSupportedError:
        pass
    else:
        raise AssertionError("Expected ProviderNotSupportedError")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_service.py -v`

Expected: FAIL with `ModuleNotFoundError` for `atv_player.danmaku.service`

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/danmaku/providers/base.py
from __future__ import annotations

from typing import Protocol

from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem


class DanmakuProvider(Protocol):
    key: str

    def search(self, name: str) -> list[DanmakuSearchItem]: ...

    def resolve(self, page_url: str) -> list[DanmakuRecord]: ...

    def supports(self, page_url: str) -> bool: ...
```

```python
# src/atv_player/danmaku/service.py
from __future__ import annotations

from dataclasses import replace

from atv_player.danmaku.errors import DanmakuEmptyResultError, ProviderNotSupportedError
from atv_player.danmaku.models import DanmakuSearchItem
from atv_player.danmaku.providers.base import DanmakuProvider
from atv_player.danmaku.utils import build_xml, match_provider, normalize_name, should_filter_name, similarity_score


class DanmakuService:
    def __init__(self, providers: dict[str, DanmakuProvider], provider_order: list[str]) -> None:
        self._providers = dict(providers)
        self._provider_order = list(provider_order)

    def _ordered_provider_keys(self, reg_src: str) -> list[str]:
        matched = match_provider(reg_src)
        if matched and matched in self._providers:
            return [matched]
        return [key for key in self._provider_order if key in self._providers]

    def search_danmu(self, name: str, reg_src: str = "") -> list[DanmakuSearchItem]:
        normalized = normalize_name(name)
        results: list[DanmakuSearchItem] = []
        for key in self._ordered_provider_keys(reg_src):
            for item in self._providers[key].search(normalized):
                if should_filter_name(normalized, item.name):
                    continue
                ratio = item.ratio or similarity_score(normalized, item.name)
                simi = item.simi or ratio
                results.append(replace(item, ratio=ratio, simi=simi))
        return sorted(results, key=lambda item: (item.ratio, item.simi, -self._provider_order.index(item.provider)), reverse=True)

    def resolve_danmu(self, page_url: str) -> str:
        for key in self._provider_order:
            provider = self._providers.get(key)
            if provider is None or not provider.supports(page_url):
                continue
            records = provider.resolve(page_url)
            if not records:
                raise DanmakuEmptyResultError(f"未找到弹幕: {page_url}")
            return build_xml(records)
        raise ProviderNotSupportedError(f"不支持的弹幕来源: {page_url}")
```

```python
# src/atv_player/danmaku/providers/__init__.py
from atv_player.danmaku.providers.base import DanmakuProvider

__all__ = ["DanmakuProvider"]
```

```python
# src/atv_player/danmaku/__init__.py
from atv_player.danmaku.service import DanmakuService

__all__ += ["DanmakuService"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_service.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_service.py src/atv_player/danmaku/service.py src/atv_player/danmaku/providers/__init__.py src/atv_player/danmaku/providers/base.py src/atv_player/danmaku/__init__.py
git commit -m "feat: add danmaku service orchestration"
```

### Task 3: Implement Tencent Provider Search And Resolution

**Files:**
- Create: `src/atv_player/danmaku/providers/tencent.py`
- Test: `tests/test_danmaku_tencent_provider.py`
- Modify: `src/atv_player/danmaku/service.py`

- [ ] **Step 1: Write the failing tests**

```python
import httpx

from atv_player.danmaku.providers.tencent import TencentDanmakuProvider


def test_tencent_provider_search_maps_candidates_from_search_payload() -> None:
    def fake_get(url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True, timeout: float = 10.0):
        assert "pbaccess.video.qq.com" in url
        return httpx.Response(
            200,
            json={
                "data": {
                    "normalList": {
                        "itemList": [
                            {
                                "videoInfo": {
                                    "title": "剑来 第1集",
                                    "url": "https://v.qq.com/x/cover/demo/vid123.html",
                                }
                            }
                        ]
                    }
                }
            },
        )

    provider = TencentDanmakuProvider(get=fake_get)

    items = provider.search("剑来 第1集")

    assert len(items) == 1
    assert items[0].provider == "tencent"
    assert items[0].name == "剑来 第1集"
    assert items[0].url == "https://v.qq.com/x/cover/demo/vid123.html"


def test_tencent_provider_resolve_extracts_video_id_and_merges_segments() -> None:
    calls: list[str] = []

    def fake_get(url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True, timeout: float = 10.0):
        calls.append(url)
        if url == "https://v.qq.com/x/cover/demo/vid123.html":
            return httpx.Response(200, text='<script>var DATA={"videoId":"vid123"};</script>')
        if "dm.video.qq.com/barrage/segment/vid123" in url and url.endswith("/0"):
            return httpx.Response(
                200,
                json={"barrage_list": [{"time_offset": 1.5, "content": "第一条", "content_style": {"position": 1, "color": 16777215}}]},
            )
        if "dm.video.qq.com/barrage/segment/vid123" in url and url.endswith("/1"):
            return httpx.Response(
                200,
                json={"barrage_list": [{"time_offset": 2.0, "content": "第二条", "content_style": {"position": 4, "color": 255}}]},
            )
        return httpx.Response(200, json={"barrage_list": []})

    provider = TencentDanmakuProvider(get=fake_get)

    records = provider.resolve("https://v.qq.com/x/cover/demo/vid123.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (1.5, 1, "16777215", "第一条"),
        (2.0, 4, "255", "第二条"),
    ]
    assert calls[0] == "https://v.qq.com/x/cover/demo/vid123.html"


def test_tencent_provider_supports_vqq_urls() -> None:
    provider = TencentDanmakuProvider()

    assert provider.supports("https://v.qq.com/x/cover/demo/vid123.html") is True
    assert provider.supports("https://v.youku.com/v_show/id_demo.html") is False


def test_tencent_provider_raises_when_video_id_is_missing() -> None:
    def fake_get(url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True, timeout: float = 10.0):
        return httpx.Response(200, text="<html>no id</html>")

    provider = TencentDanmakuProvider(get=fake_get)

    try:
        provider.resolve("https://v.qq.com/x/cover/demo/vid123.html")
    except Exception as exc:
        assert "videoId" in str(exc)
    else:
        raise AssertionError("Expected Tencent provider to reject pages without videoId")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_tencent_provider.py -v`

Expected: FAIL with `ModuleNotFoundError` for `atv_player.danmaku.providers.tencent`

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/danmaku/providers/tencent.py
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
```

```python
# src/atv_player/danmaku/service.py
from atv_player.danmaku.providers.tencent import TencentDanmakuProvider


def create_default_danmaku_service() -> DanmakuService:
    providers = {
        "tencent": TencentDanmakuProvider(),
    }
    return DanmakuService(providers, provider_order=["tencent", "youku", "iqiyi", "mgtv"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_tencent_provider.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_tencent_provider.py src/atv_player/danmaku/providers/tencent.py src/atv_player/danmaku/service.py
git commit -m "feat: add tencent danmaku provider"
```

### Task 4: Implement Youku Provider Search And Resolution

**Files:**
- Create: `src/atv_player/danmaku/providers/youku.py`
- Test: `tests/test_danmaku_youku_provider.py`
- Modify: `src/atv_player/danmaku/service.py`

- [ ] **Step 1: Write the failing tests**

```python
import httpx

from atv_player.danmaku.providers.youku import YoukuDanmakuProvider


def test_youku_provider_search_maps_candidates_from_search_payload() -> None:
    def fake_get(url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True, timeout: float = 10.0):
        assert "search.youku.com" in url
        return httpx.Response(
            200,
            json={
                "pageComponentList": [
                    {
                        "commonData": {
                            "titleDTO": {"displayName": "剑来 第1集"},
                            "updateNotice": "第1集",
                            "showId": "show123",
                            "videoLink": "https://v.youku.com/v_show/id_demo123.html",
                        }
                    }
                ]
            },
        )

    provider = YoukuDanmakuProvider(get=fake_get)

    items = provider.search("剑来 第1集")

    assert len(items) == 1
    assert items[0].provider == "youku"
    assert items[0].name == "剑来 第1集"
    assert items[0].url == "https://v.youku.com/v_show/id_demo123.html"


def test_youku_provider_resolve_extracts_vid_and_uses_data_version() -> None:
    calls: list[str] = []

    def fake_get(url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True, timeout: float = 10.0):
        calls.append(url)
        if url == "https://v.youku.com/v_show/id_demo123.html":
            return httpx.Response(200, text='{"vid":"demo123","duration":120} <div dataVersion="42"></div>')
        if "acs.youku.com/h5/mopen.youku.danmu.list/1.0/" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "result": [
                            {"playat": 1500, "propertis": '{"pos":1,"color":16777215}', "content": "优酷第一条"},
                            {"playat": 3200, "propertis": '{"pos":4,"color":255}', "content": "优酷第二条"},
                        ]
                    }
                },
            )
        raise AssertionError(url)

    provider = YoukuDanmakuProvider(get=fake_get)

    records = provider.resolve("https://v.youku.com/v_show/id_demo123.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (1.5, 1, "16777215", "优酷第一条"),
        (3.2, 4, "255", "优酷第二条"),
    ]
    assert calls[0] == "https://v.youku.com/v_show/id_demo123.html"


def test_youku_provider_supports_youku_urls() -> None:
    provider = YoukuDanmakuProvider()

    assert provider.supports("https://v.youku.com/v_show/id_demo123.html") is True
    assert provider.supports("https://v.qq.com/x/cover/demo/vid123.html") is False


def test_youku_provider_raises_when_vid_is_missing() -> None:
    def fake_get(url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True, timeout: float = 10.0):
        return httpx.Response(200, text="<html>no vid</html>")

    provider = YoukuDanmakuProvider(get=fake_get)

    try:
        provider.resolve("https://v.youku.com/v_show/id_demo123.html")
    except Exception as exc:
        assert "vid" in str(exc)
    else:
        raise AssertionError("Expected Youku provider to reject pages without vid")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_youku_provider.py -v`

Expected: FAIL with `ModuleNotFoundError` for `atv_player.danmaku.providers.youku`

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/danmaku/providers/youku.py
from __future__ import annotations

import json
import re
from urllib.parse import urlencode

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
            items = payload["pageComponentList"]
        except Exception as exc:
            raise DanmakuSearchError("优酷弹幕搜索结果解析失败") from exc
        results: list[DanmakuSearchItem] = []
        for item in items:
            common = item.get("commonData") or {}
            title = str((common.get("titleDTO") or {}).get("displayName") or "").strip()
            url = str(common.get("videoLink") or "").strip()
            if title and url:
                results.append(DanmakuSearchItem(provider=self.key, name=title, url=url))
        return results

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        response = self._get(page_url, headers={"user-agent": "Mozilla/5.0"}, follow_redirects=True, timeout=10.0)
        vid_match = re.search(r'"vid":"([^"]+)"', response.text)
        version_match = re.search(r'dataVersion="(\d+)"', response.text)
        if vid_match is None:
            raise DanmakuResolveError("优酷页面缺少 vid")
        vid = vid_match.group(1)
        data_version = version_match.group(1) if version_match else "0"
        query = urlencode({"vid": vid, "mat": 0, "mcount": 1, "type": 1, "ct": 1001, "sver": 3, "dataVersion": data_version})
        payload = self._get(
            f"https://acs.youku.com/h5/mopen.youku.danmu.list/1.0/?{query}",
            headers={"user-agent": "Mozilla/5.0", "referer": page_url},
            follow_redirects=True,
            timeout=10.0,
        ).json()
        items = ((payload.get("data") or {}).get("result") or [])
        records: list[DanmakuRecord] = []
        for item in items:
            properties = item.get("propertis") or "{}"
            parsed = json.loads(properties) if isinstance(properties, str) else dict(properties)
            records.append(
                DanmakuRecord(
                    time_offset=round(float(item.get("playat") or 0) / 1000, 3),
                    pos=int(parsed.get("pos") or 1),
                    color=str(parsed.get("color") or 16777215),
                    content=str(item.get("content") or "").strip(),
                )
            )
        return [record for record in records if record.content]
```

```python
# src/atv_player/danmaku/service.py
from atv_player.danmaku.providers.youku import YoukuDanmakuProvider


def create_default_danmaku_service() -> DanmakuService:
    providers = {
        "tencent": TencentDanmakuProvider(),
        "youku": YoukuDanmakuProvider(),
    }
    return DanmakuService(providers, provider_order=["tencent", "youku", "iqiyi", "mgtv"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_youku_provider.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_youku_provider.py src/atv_player/danmaku/providers/youku.py src/atv_player/danmaku/service.py
git commit -m "feat: add youku danmaku provider"
```

### Task 5: Add Not-Implemented Skeleton Providers, Public Exports, And Final Verification

**Files:**
- Create: `src/atv_player/danmaku/providers/iqiyi.py`
- Create: `src/atv_player/danmaku/providers/mgtv.py`
- Modify: `src/atv_player/danmaku/providers/__init__.py`
- Modify: `src/atv_player/danmaku/service.py`
- Modify: `src/atv_player/danmaku/__init__.py`
- Test: `tests/test_danmaku_service.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from atv_player.danmaku.errors import ProviderNotSupportedError
from atv_player.danmaku.service import create_default_danmaku_service


def test_default_service_has_fixed_provider_order() -> None:
    service = create_default_danmaku_service()

    assert service.provider_order == ["tencent", "youku", "iqiyi", "mgtv"]


def test_default_service_raises_clear_error_for_iqiyi_resolution() -> None:
    service = create_default_danmaku_service()

    with pytest.raises(NotImplementedError, match="iQIYI.*brotli.*protobuf"):
        service.resolve_danmu("https://www.iqiyi.com/v_demo.html")


def test_default_service_raises_clear_error_for_mgtv_resolution() -> None:
    service = create_default_danmaku_service()

    with pytest.raises(NotImplementedError, match="MGTV.*signed"):
        service.resolve_danmu("https://www.mgtv.com/b/demo/1.html")


def test_default_service_still_rejects_unknown_urls() -> None:
    service = create_default_danmaku_service()

    with pytest.raises(ProviderNotSupportedError):
        service.resolve_danmu("https://unknown.example/video/1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_service.py::test_default_service_has_fixed_provider_order tests/test_danmaku_service.py::test_default_service_raises_clear_error_for_iqiyi_resolution tests/test_danmaku_service.py::test_default_service_raises_clear_error_for_mgtv_resolution tests/test_danmaku_service.py::test_default_service_still_rejects_unknown_urls -v`

Expected: FAIL because `create_default_danmaku_service()` does not yet expose iQIYI or MGTV skeleton providers and `provider_order` is not readable.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/danmaku/providers/iqiyi.py
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem


class IqiyiDanmakuProvider:
    key = "iqiyi"

    def search(self, name: str) -> list[DanmakuSearchItem]:
        raise NotImplementedError("iQIYI danmaku search requires brotli + protobuf support")

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        raise NotImplementedError("iQIYI danmaku resolution requires brotli + protobuf support")

    def supports(self, page_url: str) -> bool:
        return "iqiyi.com" in page_url
```

```python
# src/atv_player/danmaku/providers/mgtv.py
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem


class MgtvDanmakuProvider:
    key = "mgtv"

    def search(self, name: str) -> list[DanmakuSearchItem]:
        raise NotImplementedError("MGTV danmaku search requires signed request support")

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        raise NotImplementedError("MGTV danmaku resolution requires signed request support")

    def supports(self, page_url: str) -> bool:
        return "mgtv.com" in page_url
```

```python
# src/atv_player/danmaku/providers/__init__.py
from atv_player.danmaku.providers.base import DanmakuProvider
from atv_player.danmaku.providers.iqiyi import IqiyiDanmakuProvider
from atv_player.danmaku.providers.mgtv import MgtvDanmakuProvider
from atv_player.danmaku.providers.tencent import TencentDanmakuProvider
from atv_player.danmaku.providers.youku import YoukuDanmakuProvider

__all__ = [
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
    IqiyiDanmakuProvider,
    MgtvDanmakuProvider,
    TencentDanmakuProvider,
    YoukuDanmakuProvider,
)


class DanmakuService:
    @property
    def provider_order(self) -> list[str]:
        return list(self._provider_order)


def create_default_danmaku_service() -> DanmakuService:
    providers = {
        "tencent": TencentDanmakuProvider(),
        "youku": YoukuDanmakuProvider(),
        "iqiyi": IqiyiDanmakuProvider(),
        "mgtv": MgtvDanmakuProvider(),
    }
    return DanmakuService(providers, provider_order=["tencent", "youku", "iqiyi", "mgtv"])
```

```python
# src/atv_player/danmaku/__init__.py
from atv_player.danmaku.service import DanmakuService, create_default_danmaku_service

__all__ += ["create_default_danmaku_service"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_service.py::test_default_service_has_fixed_provider_order tests/test_danmaku_service.py::test_default_service_raises_clear_error_for_iqiyi_resolution tests/test_danmaku_service.py::test_default_service_raises_clear_error_for_mgtv_resolution tests/test_danmaku_service.py::test_default_service_still_rejects_unknown_urls -v`

Expected: PASS

- [ ] **Step 5: Run the full danmaku test suite**

Run: `uv run pytest tests/test_danmaku_utils.py tests/test_danmaku_service.py tests/test_danmaku_tencent_provider.py tests/test_danmaku_youku_provider.py -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_danmaku_service.py src/atv_player/danmaku/providers/iqiyi.py src/atv_player/danmaku/providers/mgtv.py src/atv_player/danmaku/providers/__init__.py src/atv_player/danmaku/service.py src/atv_player/danmaku/__init__.py
git commit -m "feat: add danmaku provider skeletons"
```
