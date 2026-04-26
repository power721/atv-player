# Spider Plugin Drive Type Route Label Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show detected drive-provider names in spider-plugin playback route labels such as `网盘线(夸克)` while keeping normal routes unchanged.

**Architecture:** Keep provider detection and route-label formatting inside `SpiderPluginController`, because the player already renders route labels from `PlayItem.play_source`. Extend playlist construction to format drive-backed route names up front, and preserve that formatted `play_source` when lazy drive-playlist replacement happens later.

**Tech Stack:** Python 3.12, pytest, PySide6 player UI model, existing spider-plugin controller and player-window tests

---

## File Structure

- Modify: `src/atv_player/plugins/controller.py`
  Responsibility: detect drive providers from share-link hostnames, format spider route labels, and preserve formatted labels across replacement playlists.
- Modify: `tests/test_spider_plugin_controller.py`
  Responsibility: cover controller-level route-label formatting and replacement-playlist label preservation.
- Modify: `tests/test_player_window_ui.py`
  Responsibility: verify the player route selector still shows the formatted spider `play_source` label without adding player-specific formatting logic.

### Task 1: Add failing controller tests for drive-type route labels

**Files:**
- Modify: `tests/test_spider_plugin_controller.py`
- Test: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Write the failing tests**

Add these tests near the existing spider-plugin drive-playlist tests in `tests/test_spider_plugin_controller.py`:

```python
def test_controller_formats_generic_drive_route_with_detected_provider() -> None:
    spider = DriveLinkSpider()
    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=lambda link: {"list": []},
    )

    request = controller.build_request("/detail/drive")

    assert [item.play_source for item in request.playlists[0]] == ["网盘线(夸克)"]
    assert [item.play_source for item in request.playlists[1]] == ["直链线"]


def test_controller_does_not_duplicate_provider_suffix_when_route_already_names_provider() -> None:
    class BaiduDriveSpider(FakeSpider):
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "百度网盘剧集",
                        "vod_play_from": "百度线",
                        "vod_play_url": "查看$https://pan.baidu.com/s/1demo?pwd=test",
                    }
                ]
            }

    controller = SpiderPluginController(
        BaiduDriveSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=lambda link: {"list": []},
    )

    request = controller.build_request("/detail/baidu")

    assert [item.play_source for item in request.playlist] == ["百度线"]


def test_controller_preserves_formatted_drive_route_label_in_replacement_playlist() -> None:
    spider = DriveLinkSpider()

    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=lambda link: {
            "list": [
                {
                    "vod_id": "1$94954$1",
                    "vod_name": "夸克资源",
                    "items": [
                        {"title": "S1 - 1", "url": "http://m/1.mp4"},
                        {"title": "S1 - 2", "url": "http://m/2.mp4"},
                    ],
                }
            ]
        },
    )

    request = controller.build_request("/detail/drive")
    assert request.playback_loader is not None
    result = request.playback_loader(request.playlists[0][0])

    assert result is not None
    assert [item.play_source for item in result.replacement_playlist] == ["网盘线(夸克)", "网盘线(夸克)"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_spider_plugin_controller.py -k "drive_route or route_with_detected_provider or provider_suffix or preserves_formatted_drive_route_label" -v`

Expected: FAIL because `SpiderPluginController._build_playlist()` currently leaves the route labels as `网盘线` and `百度线`, and replacement playlists still inherit the unformatted `play_source`.

- [ ] **Step 3: Write minimal controller implementation**

Update `src/atv_player/plugins/controller.py` to add explicit provider-name mapping and route formatting helpers, then use them from `_build_playlist()`:

```python
_DRIVE_PROVIDER_LABELS = {
    "alipan.com": "阿里",
    "aliyundrive.com": "阿里",
    "mypikpak.com": "PikPak",
    "xunlei.com": "迅雷",
    "123pan.com": "123云盘",
    "123pan.cn": "123云盘",
    "123684.com": "123云盘",
    "123865.com": "123云盘",
    "123912.com": "123云盘",
    "123592.com": "123云盘",
    "quark.cn": "夸克",
    "139.com": "移动云盘",
    "uc.cn": "UC",
    "115.com": "115",
    "115cdn.com": "115",
    "anxia.com": "115",
    "189.cn": "天翼",
    "baidu.com": "百度",
}


def _detect_drive_provider_label(value: str) -> str:
    candidate = value.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return ""
    hostname = (urlparse(candidate).hostname or "").lower()
    for domain, label in _DRIVE_PROVIDER_LABELS.items():
        if hostname == domain or hostname.endswith(f".{domain}"):
            return label
    return ""


def _format_drive_route_label(route: str, provider: str) -> str:
    if not provider:
        return route
    normalized_route = route.strip()
    if provider in normalized_route:
        return normalized_route
    return f"{normalized_route}({provider})"
```

Then change `_build_playlist()` so each group computes its route label before creating items:

```python
            route = self._route_name(routes, group_index)
            route_label = route
            ...
                if is_drive_link and not _looks_like_media_url(clean_value):
                    provider = _detect_drive_provider_label(clean_value)
                    if provider:
                        route_label = _format_drive_route_label(route, provider)
                playlist.append(
                    PlayItem(
                        ...
                        play_source=route_label,
                    )
                )
```

Do not change `_build_drive_replacement_playlist()`: it should keep inheriting the clicked item's already formatted `play_source`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_spider_plugin_controller.py -k "drive_route or route_with_detected_provider or provider_suffix or preserves_formatted_drive_route_label" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_spider_plugin_controller.py src/atv_player/plugins/controller.py
git commit -m "feat: label spider drive routes with provider names"
```

### Task 2: Add failing player-window test for route selector display

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing test**

Add this test near the existing route-replacement tests in `tests/test_player_window_ui.py`:

```python
def test_player_window_route_selector_uses_formatted_spider_play_source_label(qtbot) -> None:
    controller = FakePlayerController()
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘剧集"),
        playlist=[PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="网盘线(夸克)")],
        playlists=[
            [PlayItem(title="第1集", url="http://line/1.m3u8", play_source="播放源 1")],
            [PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="网盘线(夸克)")],
        ],
        playlist_index=1,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window = PlayerWindow(controller, config=None, save_config=lambda: None)
    qtbot.addWidget(window)

    window.open_session(session)

    assert window.playlist_group_combo.count() == 2
    assert window.playlist_group_combo.itemText(0) == "播放源 1"
    assert window.playlist_group_combo.itemText(1) == "网盘线(夸克)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_route_selector_uses_formatted_spider_play_source_label -v`

Expected: FAIL initially if the controller work from Task 1 is not present yet in the same branch, or pass immediately once Task 1 is already implemented. If it passes immediately, keep the test and proceed without player production edits because that proves the current player boundary is correct.

- [ ] **Step 3: Write minimal implementation**

No player production-code change is expected if Task 1 is complete. The current route selector should already render from `PlayItem.play_source`:

```python
    def _playlist_group_label(self, playlist: list[PlayItem], playlist_index: int) -> str:
        if playlist and playlist[0].play_source:
            return playlist[0].play_source
        return f"线路 {playlist_index + 1}"
```

If the test exposes a real mismatch, make only the minimal change needed in `src/atv_player/ui/player_window.py` to keep using `play_source` verbatim instead of adding any provider-detection logic there.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_route_selector_uses_formatted_spider_play_source_label -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "test: cover formatted spider drive route labels in player selector"
```

### Task 3: Run focused regression verification

**Files:**
- Modify: none
- Test: `tests/test_spider_plugin_controller.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Run focused controller and player tests**

Run: `uv run pytest tests/test_spider_plugin_controller.py tests/test_player_window_ui.py -k "spider or route_selector_uses_formatted_spider_play_source_label or replacement" -v`

Expected: PASS for the updated spider-plugin drive tests and the player-window route-label test, with no regressions in the existing replacement-playlist coverage.

- [ ] **Step 2: Run a narrow grep-based self-check**

Run: `rg -n "网盘线\\(夸克\\)|百度线|play_source" src/atv_player/plugins/controller.py tests/test_spider_plugin_controller.py tests/test_player_window_ui.py`

Expected: output shows the new formatted-label expectations in tests and the single controller implementation path responsible for assigning formatted `play_source`.

- [ ] **Step 3: Commit final verification state**

```bash
git add src/atv_player/plugins/controller.py tests/test_spider_plugin_controller.py tests/test_player_window_ui.py
git commit -m "test: verify spider drive route label regressions"
```
