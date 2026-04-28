# Drive Danmaku Episode Inference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Infer the correct drive-playlist episode number for danmaku search by combining current title parsing with replacement-playlist position fallback.

**Architecture:** Keep episode parsing and inference in `atv_player.danmaku.utils` so the logic is deterministic and testable. Update `SpiderPluginController` to pass replacement-playlist context when building drive danmaku search names, without changing the external danmaku service API or non-drive playback behavior.

**Tech Stack:** Python 3.14, pytest, existing `atv_player.danmaku` and plugin controller modules

---

## File Structure

- Modify: `src/atv_player/danmaku/utils.py`
  Responsibility: parse more episode title formats and infer an episode from playlist context.
- Modify: `src/atv_player/plugins/controller.py`
  Responsibility: build drive danmaku search names from current item plus replacement-playlist context.
- Modify: `tests/test_danmaku_utils.py`
  Responsibility: lock the parsing and playlist-inference contract.
- Modify: `tests/test_spider_plugin_controller.py`
  Responsibility: lock drive danmaku search behavior for parsed-title and index-fallback cases.

### Task 1: Define Playlist-Aware Episode Inference In Tests

**Files:**
- Modify: `tests/test_danmaku_utils.py`
- Modify: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Write the failing utility tests**

Add these tests to `tests/test_danmaku_utils.py`:

```python
from atv_player.models import PlayItem
from atv_player.danmaku.utils import extract_episode_number, infer_playlist_episode_number


def test_extract_episode_number_supports_chinese_numerals() -> None:
    assert extract_episode_number("第十二集") == 12


def test_extract_episode_number_supports_zero_padded_prefix_titles() -> None:
    assert extract_episode_number("0002 剑来-笼中雀") == 2


def test_infer_playlist_episode_number_prefers_current_title() -> None:
    playlist = [
        PlayItem(title="0001 剑来-总管坐镇剑气长城", url="http://m/1.mp4", index=0),
        PlayItem(title="0002 剑来-笼中雀", url="http://m/2.mp4", index=1),
        PlayItem(title="0003 剑来-第三集", url="http://m/3.mp4", index=2),
    ]

    assert infer_playlist_episode_number(playlist[1], playlist) == 2


def test_infer_playlist_episode_number_falls_back_to_playlist_position() -> None:
    playlist = [
        PlayItem(title="正片.mp4", url="http://m/1.mp4", index=0),
        PlayItem(title="国语.mp4", url="http://m/2.mp4", index=1),
        PlayItem(title="超清.mp4", url="http://m/3.mp4", index=2),
    ]

    assert infer_playlist_episode_number(playlist[1], playlist) == 2
```

- [ ] **Step 2: Write the failing drive-controller regression test**

Add this test near the existing drive danmaku tests in `tests/test_spider_plugin_controller.py`:

```python
def test_controller_uses_replacement_playlist_index_when_drive_titles_have_no_episode_number() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return []

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.quark.cn/s/f518510ef92a"
        return {
            "list": [
                {
                    "vod_id": "1$94954$1",
                    "vod_name": "夸克资源",
                    "items": [
                        {"title": "正片.mp4", "url": "http://m/1.mp4", "path": "/S1/1.mp4", "size": 11},
                        {"title": "国语.mp4", "url": "http://m/2.mp4", "path": "/S1/2.mp4", "size": 12},
                        {"title": "超清.mp4", "url": "http://m/3.mp4", "path": "/S1/3.mp4", "size": 13},
                    ],
                }
            ]
        }

    controller = SpiderPluginController(
        DanmakuDriveLinkSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/drive")
    result = request.playback_loader(request.playlists[0][0])

    assert result is not None
    request.playback_loader(result.replacement_playlist[1])

    assert calls == [
        ("search", "网盘剧集 1集|https://pan.quark.cn/s/f518510ef92a"),
        ("search", "网盘剧集 2集|http://m/2.mp4"),
    ]
```

- [ ] **Step 3: Run the targeted tests to verify they fail**

Run: `uv run pytest tests/test_danmaku_utils.py tests/test_spider_plugin_controller.py -q`

Expected: failures showing `extract_episode_number()` does not yet parse Chinese numerals or zero-padded prefix titles, and drive danmaku search still falls back to media-title-only lookup for non-episodic filenames.

### Task 2: Implement Playlist-Aware Inference In `utils.py`

**Files:**
- Modify: `src/atv_player/danmaku/utils.py`
- Modify: `tests/test_danmaku_utils.py`

- [ ] **Step 1: Add Chinese numeral parsing support**

Add a helper like this near the episode-pattern definitions:

```python
_CN_NUM = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def _cn_to_int(text: str) -> int | None:
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + _CN_NUM.get(text[1], 0)
    if text.endswith("十"):
        return _CN_NUM.get(text[0], 0) * 10
    if "十" in text:
        left, right = text.split("十", 1)
        return _CN_NUM.get(left, 0) * 10 + _CN_NUM.get(right, 0)
    if text in _CN_NUM:
        return _CN_NUM[text]
    return None
```

- [ ] **Step 2: Expand `extract_episode_number()` patterns**

Update `_EPISODE_PATTERNS` and `extract_episode_number()` so they accept:

```python
_EPISODE_PATTERNS = (
    r"第\s*([0-9零一二两三四五六七八九十]+)\s*[集话期部回]",
    r"\bS\d+\s*E0*([0-9]+)\b",
    r"\bEP\s*0*([0-9]+)\b",
    r"\bE\s*0*([0-9]+)\b",
    r"^\s*0*([0-9]{1,4})\b",
    r"^\s*(\d+)\s*(?:[（(][^()（）]*[)）])?\s*$",
)
```

And implement value conversion like this:

```python
def extract_episode_number(name: str) -> int | None:
    value = normalize_name(name)
    for pattern in _EPISODE_PATTERNS:
        match = re.search(pattern, value, re.IGNORECASE)
        if match is None:
            continue
        raw = match.group(1)
        episode = int(raw) if raw.isdigit() else _cn_to_int(raw)
        if episode is not None and 1 <= episode <= 10000:
            return episode
    return None
```

- [ ] **Step 3: Add playlist-aware inference helper**

Add a focused helper that stays independent from controller code:

```python
from atv_player.models import PlayItem


def infer_playlist_episode_number(current_item: PlayItem, playlist: Sequence[PlayItem] | None = None) -> int | None:
    direct = extract_episode_number(current_item.title)
    if direct is not None:
        return direct
    if not playlist:
        return current_item.index + 1 if current_item.index >= 0 else None
    current_index = current_item.index
    if 0 <= current_index < len(playlist):
        indexed = extract_episode_number(playlist[current_index].title)
        if indexed is not None:
            return indexed
    known = [(item.index, extract_episode_number(item.title)) for item in playlist]
    aligned = [(index, episode) for index, episode in known if episode is not None]
    if aligned:
        seq_like = sum(1 for index, episode in aligned if episode == index + 1)
        if seq_like >= max(1, len(aligned) // 2):
            return current_index + 1 if current_index >= 0 else None
    return current_index + 1 if current_index >= 0 else None
```

- [ ] **Step 4: Run the utility tests to verify they pass**

Run: `uv run pytest tests/test_danmaku_utils.py -q`

Expected: all utility tests pass, including Chinese numerals, zero-padded prefix titles, and playlist fallback cases.

### Task 3: Wire Replacement-Playlist Context Into The Plugin Controller

**Files:**
- Modify: `src/atv_player/plugins/controller.py`
- Modify: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Update danmaku search-name helpers to accept playlist context**

Change the helper layer near the top of `src/atv_player/plugins/controller.py` to:

```python
from atv_player.danmaku.utils import extract_episode_number, infer_playlist_episode_number


def _extract_episode_label(item: PlayItem, playlist: list[PlayItem] | None = None) -> str:
    episode_number = infer_playlist_episode_number(item, playlist)
    if episode_number is None:
        return ""
    return f"{episode_number}集"


def _build_danmaku_search_name(item: PlayItem, playlist: list[PlayItem] | None = None) -> str:
    media_title = item.media_title.strip()
    if not media_title:
        return item.title.strip()
    episode_label = _extract_episode_label(item, playlist)
    return " ".join(part for part in (media_title, episode_label) if part).strip()
```

- [ ] **Step 2: Pass replacement playlists into danmaku resolution**

Update the danmaku resolution path so replacement items are searched with their playlist context:

```python
def _resolve_danmaku_sync(self, item: PlayItem, url: str, playlist: list[PlayItem] | None = None) -> None:
    if not self._danmaku_enabled or self._danmaku_service is None:
        return
    search_name = _build_danmaku_search_name(item, playlist)
    ...


def _maybe_resolve_danmaku(self, item: PlayItem, url: str, playlist: list[PlayItem] | None = None) -> None:
    ...
    def run() -> None:
        try:
            self._resolve_danmaku_sync(item, url, playlist)
        finally:
            ...
```

Then pass the replacement playlist in these call sites:

```python
self._maybe_resolve_danmaku(replacement[replacement_start_index], item.vod_id, replacement)
...
self._maybe_resolve_danmaku(replacement[replacement_start_index], url, replacement)
```

Leave existing non-drive `self._maybe_resolve_danmaku(item, item.url)` and parse-path calls unchanged so only drive replacement lists gain the new inference behavior.

- [ ] **Step 3: Run the targeted controller tests to verify they pass**

Run: `uv run pytest tests/test_spider_plugin_controller.py -q`

Expected: the drive danmaku tests pass, including the new index-fallback regression.

### Task 4: Run Final Verification

**Files:**
- Test: `tests/test_danmaku_utils.py`
- Test: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Run the combined verification slice**

Run: `uv run pytest tests/test_danmaku_utils.py tests/test_spider_plugin_controller.py -q`

Expected: all selected tests pass with no new failures.

- [ ] **Step 2: Commit the implementation**

Run:

```bash
git add src/atv_player/danmaku/utils.py src/atv_player/plugins/controller.py tests/test_danmaku_utils.py tests/test_spider_plugin_controller.py
git commit -m "feat: infer drive danmaku episode from playlist context"
```

Expected: one commit containing the test-first implementation for drive danmaku episode inference.
