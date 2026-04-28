# MGTV Danmaku Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stubbed `MgtvDanmakuProvider` with a working MGTV danmaku provider that searches candidate episodes, resolves play URLs into danmaku records, and keeps the existing `DanmakuService` contract unchanged.

**Architecture:** Keep all MGTV-specific logic inside `src/atv_player/danmaku/providers/mgtv.py`. Implement search parsing and collection expansion first, then add URL parsing, segmented danmaku resolution, and comment formatting with a `getctlbarrage` primary path and `rdbarrage` fallback path. Verify everything through deterministic provider tests with mocked HTTP responses.

**Tech Stack:** Python 3.12, httpx, urllib.parse, re, pytest

---

### Task 1: Replace The MGTV Stub With Search Parsing

**Files:**
- Create: `tests/test_danmaku_mgtv_provider.py`
- Modify: `src/atv_player/danmaku/providers/mgtv.py`

- [ ] **Step 1: Write the failing tests**

```python
import httpx
import pytest

from atv_player.danmaku.errors import DanmakuSearchError
from atv_player.danmaku.providers.mgtv import MgtvDanmakuProvider


def test_mgtv_search_filters_non_imgo_and_invalid_urls() -> None:
    def fake_get(url: str, **kwargs):
        assert url == "https://mobileso.bz.mgtv.com/msite/search/v2"
        assert kwargs["params"]["q"] == "歌手2026"
        return httpx.Response(
            200,
            json={
                "data": {
                    "contents": [
                        {
                            "type": "media",
                            "data": [
                                {
                                    "source": "imgo",
                                    "title": "<em>歌手2026</em>",
                                    "url": "https://www.mgtv.com/b/777/1.html",
                                },
                                {
                                    "source": "other",
                                    "title": "外站结果",
                                    "url": "https://example.com/b/888/1.html",
                                },
                                {
                                    "source": "imgo",
                                    "title": "坏结果",
                                    "url": "https://www.mgtv.com/not-a-play-url.html",
                                },
                            ],
                        }
                    ]
                }
            },
        )

    provider = MgtvDanmakuProvider(get=fake_get)
    provider._expand_candidate = lambda title, collection_id: []

    items = provider.search("歌手2026")

    assert items == []


def test_mgtv_search_expands_collection_into_episode_candidates() -> None:
    def fake_get(url: str, **kwargs):
        return httpx.Response(
            200,
            json={
                "data": {
                    "contents": [
                        {
                            "type": "media",
                            "data": [
                                {
                                    "source": "imgo",
                                    "title": "<em>歌手2026</em>",
                                    "url": "https://www.mgtv.com/b/555/1.html",
                                }
                            ],
                        }
                    ]
                }
            },
        )

    provider = MgtvDanmakuProvider(get=fake_get)
    provider._expand_candidate = lambda title, collection_id: [
        ("歌手2026 第1期", "https://www.mgtv.com/b/555/1001.html"),
        ("歌手2026 第2期", "https://www.mgtv.com/b/555/1002.html"),
    ]

    items = provider.search("歌手2026")

    assert [(item.provider, item.name, item.url) for item in items] == [
        ("mgtv", "歌手2026 第1期", "https://www.mgtv.com/b/555/1001.html"),
        ("mgtv", "歌手2026 第2期", "https://www.mgtv.com/b/555/1002.html"),
    ]


def test_mgtv_search_raises_for_invalid_payload() -> None:
    provider = MgtvDanmakuProvider(get=lambda url, **kwargs: httpx.Response(200, json={"oops": 1}))

    with pytest.raises(DanmakuSearchError, match="MGTV"):
        provider.search("歌手2026")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_mgtv_provider.py -k "search" -v`

Expected: FAIL because `MgtvDanmakuProvider.search()` currently raises `NotImplementedError`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/danmaku/providers/mgtv.py
from __future__ import annotations

import re

import httpx

from atv_player.danmaku.errors import DanmakuSearchError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem


class MgtvDanmakuProvider:
    key = "mgtv"

    def __init__(self, get=httpx.get) -> None:
        self._get = get

    def search(self, name: str) -> list[DanmakuSearchItem]:
        response = self._get(
            "https://mobileso.bz.mgtv.com/msite/search/v2",
            params={"q": name},
            headers=self._json_headers(),
            follow_redirects=True,
            timeout=10.0,
        )
        payload = response.json()
        contents = ((payload.get("data") or {}).get("contents"))
        if not isinstance(contents, list):
            raise DanmakuSearchError("MGTV danmaku search failed: invalid payload")
        results: list[DanmakuSearchItem] = []
        for content in contents:
            if str(content.get("type") or "") != "media":
                continue
            for item in content.get("data") or []:
                if str(item.get("source") or "") != "imgo":
                    continue
                raw_url = str(item.get("url") or "")
                match = re.search(r"/b/(\d+)", raw_url)
                if match is None:
                    continue
                title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
                results.extend(
                    DanmakuSearchItem(provider=self.key, name=episode_name, url=episode_url)
                    for episode_name, episode_url in self._expand_candidate(title, match.group(1))
                )
        return results

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        raise NotImplementedError

    def supports(self, page_url: str) -> bool:
        return "mgtv.com" in page_url

    def _expand_candidate(self, title: str, collection_id: str) -> list[tuple[str, str]]:
        return []

    def _json_headers(self) -> dict[str, str]:
        return {
            "user-agent": "Mozilla/5.0",
            "referer": "https://www.mgtv.com/",
            "accept": "application/json",
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_mgtv_provider.py -k "search" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_mgtv_provider.py src/atv_player/danmaku/providers/mgtv.py
git commit -m "feat: add mgtv danmaku search parsing"
```

### Task 2: Expand Collection Hits Into Episodes And Movies

**Files:**
- Modify: `tests/test_danmaku_mgtv_provider.py`
- Modify: `src/atv_player/danmaku/providers/mgtv.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_mgtv_search_expands_month_tabs_and_filters_preview_titles() -> None:
    calls: list[tuple[str, dict]] = []

    def fake_get(url: str, **kwargs):
        calls.append((url, kwargs.get("params") or {}))
        if "msite/search/v2" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "contents": [
                            {
                                "type": "media",
                                "data": [
                                    {
                                        "source": "imgo",
                                        "title": "歌手2026",
                                        "url": "https://www.mgtv.com/b/555/1.html",
                                    }
                                ],
                            }
                        ]
                    }
                },
            )
        if "variety/showlist" in url and kwargs["params"].get("month", "") == "":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "tab_m": [{"m": "2026-04"}, {"m": "2026-05"}],
                        "list": [
                            {"t2": "第1期", "t1": "", "video_id": "1001", "isnew": "1", "src_clip_id": "555"},
                            {"t2": "第1期 预告", "t1": "", "video_id": "100x", "isnew": "2", "src_clip_id": "555"},
                        ],
                    }
                },
            )
        if "variety/showlist" in url and kwargs["params"].get("month") == "2026-05":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "tab_m": [{"m": "2026-04"}, {"m": "2026-05"}],
                        "list": [
                            {"t2": "第2期", "t1": "", "video_id": "1002", "isnew": "1", "src_clip_id": "555"},
                        ],
                    }
                },
            )
        raise AssertionError((url, kwargs))

    provider = MgtvDanmakuProvider(get=fake_get)

    items = provider.search("歌手2026")

    assert [(item.name, item.url) for item in items] == [
        ("歌手2026 第1期", "https://www.mgtv.com/b/555/1001.html"),
        ("歌手2026 第2期", "https://www.mgtv.com/b/555/1002.html"),
    ]
    assert [params.get("month", "") for _, params in calls if "variety/showlist" in _] == ["", "2026-05"]


def test_mgtv_expand_candidate_selects_best_movie_item() -> None:
    def fake_get(url: str, **kwargs):
        if "variety/showlist" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "list": [
                            {"t3": "预告", "video_id": "9000", "isnew": "2"},
                            {"t3": "正片", "video_id": "9001", "isIntact": "1"},
                        ]
                    }
                },
            )
        raise AssertionError((url, kwargs))

    provider = MgtvDanmakuProvider(get=fake_get)

    assert provider._expand_candidate("电影名", "777") == [
        ("电影名 正片", "https://www.mgtv.com/b/777/9001.html")
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_mgtv_provider.py -k "expands_month_tabs or selects_best_movie" -v`

Expected: FAIL because `_expand_candidate()` still returns an empty list.

- [ ] **Step 3: Write minimal implementation**

```python
def _expand_candidate(self, title: str, collection_id: str) -> list[tuple[str, str]]:
    episodes = self._collection_items(collection_id)
    if not episodes:
        return []
    if len(episodes) == 1:
        best = self._pick_movie_item(episodes)
        return [(self._candidate_name(title, best), self._episode_url(collection_id, str(best.get("video_id") or "")))]
    kept = []
    for episode in episodes:
        if str(episode.get("src_clip_id") or collection_id) != collection_id:
            continue
        if str(episode.get("isnew") or "") == "2":
            continue
        full_title = self._episode_title(episode)
        if self._is_noise_title(full_title):
            continue
        kept.append((f"{title} {full_title}".strip(), self._episode_url(collection_id, str(episode.get('video_id') or ''))))
    return kept


def _collection_items(self, collection_id: str) -> list[dict]:
    months = [""]
    output: list[dict] = []
    for index, month in enumerate(months):
        response = self._get(
            "https://pcweb.api.mgtv.com/variety/showlist",
            params={"collection_id": collection_id, "month": month, "page": 1, "allowedRC": 1, "_support": 10000000},
            headers=self._json_headers(),
            follow_redirects=True,
            timeout=10.0,
        )
        payload = response.json()
        data = payload.get("data") or {}
        if index == 0:
            months.extend(tab.get("m", "") for tab in data.get("tab_m") or [][1:] if tab.get("m"))
        output.extend(data.get("list") or [])
    return output
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_mgtv_provider.py -k "expands_month_tabs or selects_best_movie" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_mgtv_provider.py src/atv_player/danmaku/providers/mgtv.py
git commit -m "feat: expand mgtv collections into danmaku candidates"
```

### Task 3: Resolve MGTV Play URLs Through CDN And Fallback Barrage APIs

**Files:**
- Modify: `tests/test_danmaku_mgtv_provider.py`
- Modify: `src/atv_player/danmaku/providers/mgtv.py`

- [ ] **Step 1: Write the failing tests**

```python
from atv_player.danmaku.errors import DanmakuResolveError


def test_mgtv_resolve_uses_cdn_segments_when_control_metadata_exists() -> None:
    def fake_get(url: str, **kwargs):
        if "video/info" in url:
            return httpx.Response(200, json={"data": {"info": {"time": "01:35"}}})
        if "getctlbarrage" in url:
            return httpx.Response(200, json={"data": {"cdn_list": "bullet.mgtv.com,backup.mgtv.com", "cdn_version": "v2"}})
        if url == "https://bullet.mgtv.com/v2/0.json":
            return httpx.Response(
                200,
                json={"data": {"items": [{"time": 1500, "content": "第一条", "v2_position": 1, "v2_color": {"color_left": "rgb(255,0,0)", "color_right": "rgb(255,0,0)"}}]}},
            )
        if url == "https://bullet.mgtv.com/v2/1.json":
            return httpx.Response(
                200,
                json={"data": {"items": [{"time": 61000, "content": "第二条", "v2_position": 2, "v2_color": {"color_left": "rgb(0,255,0)", "color_right": "rgb(0,255,0)"}}]}},
            )
        raise AssertionError((url, kwargs))

    provider = MgtvDanmakuProvider(get=fake_get)

    records = provider.resolve("https://www.mgtv.com/b/555/1001.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (1.5, 5, "16711680", "第一条"),
        (61.0, 4, "65280", "第二条"),
    ]


def test_mgtv_resolve_falls_back_to_rdbarrage_when_control_metadata_is_missing() -> None:
    requested: list[str] = []

    def fake_get(url: str, **kwargs):
        requested.append(url)
        if "video/info" in url:
            return httpx.Response(200, json={"data": {"info": {"time": "00:59"}}})
        if "getctlbarrage" in url:
            return httpx.Response(200, json={"data": {}})
        if "rdbarrage" in url:
            return httpx.Response(200, json={"data": {"items": [{"time": 3000, "content": "回退路径"}]}})
        raise AssertionError((url, kwargs))

    provider = MgtvDanmakuProvider(get=fake_get)

    records = provider.resolve("https://www.mgtv.com/b/555/1001.html")

    assert [(record.time_offset, record.content) for record in records] == [(3.0, "回退路径")]
    assert any("rdbarrage" in url for url in requested)


def test_mgtv_resolve_rejects_invalid_play_url() -> None:
    provider = MgtvDanmakuProvider(get=lambda url, **kwargs: httpx.Response(200, json={}))

    with pytest.raises(DanmakuResolveError, match="MGTV"):
        provider.resolve("https://www.mgtv.com/not-valid.html")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_mgtv_provider.py -k "resolve" -v`

Expected: FAIL because `resolve()` is not implemented.

- [ ] **Step 3: Write minimal implementation**

```python
from atv_player.danmaku.errors import DanmakuResolveError
from atv_player.danmaku.models import DanmakuRecord


def resolve(self, page_url: str) -> list[DanmakuRecord]:
    collection_id, video_id = self._parse_play_url(page_url)
    duration = self._video_duration(collection_id, video_id)
    segment_urls = self._segment_urls(collection_id, video_id, duration)
    records: list[DanmakuRecord] = []
    for segment_url in segment_urls:
        response = self._get(
            segment_url,
            headers=self._json_headers(),
            follow_redirects=True,
            timeout=10.0,
        )
        payload = response.json()
        records.extend(self._segment_records(payload))
    return records


def _parse_play_url(self, page_url: str) -> tuple[str, str]:
    match = re.search(r"/b/(\d+)/([^/?#]+)\.html", page_url)
    if match is None:
        raise DanmakuResolveError("MGTV danmaku resolve failed: invalid play url")
    return match.group(1), match.group(2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_mgtv_provider.py -k "resolve" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_mgtv_provider.py src/atv_player/danmaku/providers/mgtv.py
git commit -m "feat: resolve mgtv danmaku segments"
```

### Task 4: Harden MGTV Formatting And Service Integration

**Files:**
- Modify: `tests/test_danmaku_mgtv_provider.py`
- Modify: `tests/test_danmaku_service.py`
- Modify: `src/atv_player/danmaku/providers/mgtv.py`

- [ ] **Step 1: Write the failing tests**

```python
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem
from atv_player.danmaku.service import DanmakuService


def test_mgtv_provider_supports_only_mgtv_urls() -> None:
    provider = MgtvDanmakuProvider()

    assert provider.supports("https://www.mgtv.com/b/555/1001.html") is True
    assert provider.supports("https://v.qq.com/x/cover/demo/vid123.html") is False


def test_mgtv_resolve_ignores_empty_segment_items() -> None:
    def fake_get(url: str, **kwargs):
        if "video/info" in url:
            return httpx.Response(200, json={"data": {"info": {"time": "00:30"}}})
        if "getctlbarrage" in url:
            return httpx.Response(200, json={"data": {"cdn_list": "bullet.mgtv.com", "cdn_version": "v1"}})
        if url == "https://bullet.mgtv.com/v1/0.json":
            return httpx.Response(200, json={"data": {"items": [{"time": 1000, "content": ""}, {"time": 2000, "content": "保留"}]}})
        raise AssertionError((url, kwargs))

    provider = MgtvDanmakuProvider(get=fake_get)

    records = provider.resolve("https://www.mgtv.com/b/555/1001.html")

    assert records == [DanmakuRecord(time_offset=2.0, pos=1, color="16777215", content="保留")]


def test_service_resolve_danmu_uses_mgtv_provider_for_mgtv_urls() -> None:
    provider = FakeProvider(
        "mgtv",
        [DanmakuSearchItem(provider="mgtv", name="歌手2026 第1期", url="https://www.mgtv.com/b/555/1001.html")],
        [DanmakuRecord(time_offset=1.5, pos=1, color="16777215", content="芒果弹幕")],
    )
    service = DanmakuService({"mgtv": provider}, provider_order=["mgtv"])

    xml_text = service.resolve_danmu("https://www.mgtv.com/b/555/1001.html")

    assert "<d p=\"1.5,1,25,16777215\">芒果弹幕</d>" in xml_text
    assert provider.resolve_calls == ["https://www.mgtv.com/b/555/1001.html"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_mgtv_provider.py tests/test_danmaku_service.py -k "mgtv" -v`

Expected: FAIL because the provider does not yet discard empty comments or may not fully integrate with service expectations.

- [ ] **Step 3: Write minimal implementation**

```python
def _segment_records(self, payload: dict) -> list[DanmakuRecord]:
    items = ((payload.get("data") or {}).get("items") or [])
    records: list[DanmakuRecord] = []
    for item in items:
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        records.append(
            DanmakuRecord(
                time_offset=round(float(item.get("time") or 0) / 1000, 3),
                pos=self._comment_pos(item),
                color=str(self._comment_color(item)),
                content=content,
            )
        )
    return records
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_mgtv_provider.py tests/test_danmaku_service.py -k "mgtv" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_mgtv_provider.py tests/test_danmaku_service.py src/atv_player/danmaku/providers/mgtv.py
git commit -m "feat: finalize mgtv danmaku provider"
```

### Task 5: Full Verification

**Files:**
- Modify: `tests/test_danmaku_mgtv_provider.py` only if a final red/green fix is needed
- Modify: `src/atv_player/danmaku/providers/mgtv.py` only if a final red/green fix is needed

- [ ] **Step 1: Run focused provider and service verification**

Run: `uv run pytest tests/test_danmaku_mgtv_provider.py tests/test_danmaku_service.py -v`

Expected: PASS with MGTV provider tests covering search, expansion, resolve, fallback, and service dispatch.

- [ ] **Step 2: Run the broader danmaku provider regression suite**

Run: `uv run pytest tests/test_danmaku_tencent_provider.py tests/test_danmaku_youku_provider.py tests/test_danmaku_iqiyi_provider.py tests/test_danmaku_bilibili_provider.py tests/test_danmaku_service.py -v`

Expected: PASS, confirming the MGTV implementation did not break existing provider behavior.

- [ ] **Step 3: Commit final polish if verification required any follow-up**

```bash
git add tests/test_danmaku_mgtv_provider.py src/atv_player/danmaku/providers/mgtv.py tests/test_danmaku_service.py
git commit -m "test: verify mgtv danmaku provider coverage"
```
