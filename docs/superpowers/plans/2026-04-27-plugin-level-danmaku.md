# Plugin-Level Danmaku Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch spider-plugin danmaku enablement from `playerContent()["danmu"]` to plugin-level `Spider.danmaku()`, and ignore the old payload flag completely.

**Architecture:** Keep the change localized to `SpiderPluginController`. Read `Spider.danmaku()` once during controller initialization, cache it as a private boolean capability flag, and gate `_maybe_resolve_danmaku()` with that flag instead of inspecting `playerContent()` payloads. The compat base spider already provides `danmaku() -> False`, so no database, loader, or UI changes are needed.

**Tech Stack:** Python 3.14, pytest, existing spider plugin compatibility layer in `src/atv_player/plugins/compat/base/spider.py`

---

## File Structure

- Modify: `src/atv_player/plugins/controller.py`
  Responsibility: cache plugin-level danmaku capability and use it as the only danmaku trigger during playback resolution.
- Test: `tests/test_spider_plugin_controller.py`
  Responsibility: verify danmaku resolves only when `Spider.danmaku()` returns `True`, and verify legacy `playerContent()["danmu"]` no longer has any effect.
- Reference only: `src/atv_player/plugins/compat/base/spider.py`
  Responsibility: already provides `danmaku()` defaulting to `False`; do not change it unless the local uncommitted edit in that file has removed the method.

### Task 1: Update Controller Tests To Define The New Contract

**Files:**
- Modify: `tests/test_spider_plugin_controller.py`
- Test: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Write the failing test for plugin-level enablement**

Replace the legacy spider fixture that returns `"danmu": True` from `playerContent()` with a spider that enables danmaku through `danmaku()`.

```python
class PluginLevelDanmakuSpider(FakeSpider):
    def playerContent(self, flag, id, vipFlags):
        return {"parse": 0, "url": f"https://stream.example{id}.m3u8"}

    def danmaku(self):
        return True
```

Rename the existing resolution test to reflect the new contract and keep the assertion body the same apart from the fixture name:

```python
def test_controller_resolves_danmaku_when_spider_enables_plugin_level_danmaku() -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return [DanmakuSearchItem(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/x/cover/demo.html")]

        def resolve_danmu(self, page_url: str) -> str:
            calls.append(("resolve", page_url))
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">hi</d></i>'

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.danmaku_xml == '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">hi</d></i>'
    assert calls == [
        ("search", "红果短剧 第1集|/play/1"),
        ("resolve", "https://v.qq.com/x/cover/demo.html"),
    ]
```

- [ ] **Step 2: Write the failing test that rejects legacy `playerContent()["danmu"]`**

Add a fixture that still returns `"danmu": True` but does not override `danmaku()`:

```python
class LegacyPayloadDanmuSpider(FakeSpider):
    def playerContent(self, flag, id, vipFlags):
        return {"parse": 0, "danmu": True, "url": f"https://stream.example{id}.m3u8"}
```

Add a new negative test:

```python
def test_controller_ignores_legacy_player_content_danmu_flag_when_plugin_level_danmaku_is_disabled() -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return [DanmakuSearchItem(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/x/cover/demo.html")]

        def resolve_danmu(self, page_url: str) -> str:
            calls.append(("resolve", page_url))
            return "unexpected"

    controller = SpiderPluginController(
        LegacyPayloadDanmuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.danmaku_xml == ""
    assert calls == []
```

- [ ] **Step 3: Update the failure-tolerant danmaku test to use plugin-level enablement**

Change the fixture used by the existing failure test from the legacy payload-driven spider to `PluginLevelDanmakuSpider()`:

```python
def test_controller_ignores_danmaku_resolution_failures_without_breaking_playback(caplog) -> None:
    class FailingDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            raise RuntimeError("danmu boom")

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FailingDanmakuService(),
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    with caplog.at_level(logging.WARNING):
        request.playback_loader(first)

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.danmaku_xml == ""
    assert "danmaku" in caplog.text.lower()
```

- [ ] **Step 4: Run the targeted test file and verify it fails for the right reason**

Run: `uv run pytest tests/test_spider_plugin_controller.py -k 'danmaku' -v`

Expected: FAIL because `SpiderPluginController._maybe_resolve_danmaku()` still checks `payload.get("danmu")`, so the new plugin-level enablement test should not resolve danmaku yet and the legacy-payload rejection test should still incorrectly trigger the service.

- [ ] **Step 5: Commit the red test change**

```bash
git add tests/test_spider_plugin_controller.py
git commit -m "test: define plugin-level danmaku toggle behavior"
```

### Task 2: Implement Plugin-Level Danmaku Gating In The Controller

**Files:**
- Modify: `src/atv_player/plugins/controller.py:116-136`
- Modify: `src/atv_player/plugins/controller.py:278-302`
- Modify: `src/atv_player/plugins/controller.py:386-400`
- Test: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Add a cached controller-level danmaku capability flag**

Patch the controller initializer to read `Spider.danmaku()` once and normalize it to `bool`. Preserve any unrelated local edits in this file; only change the constructor state initialization.

```python
class SpiderPluginController:
    def __init__(
        self,
        spider,
        plugin_name: str,
        search_enabled: bool,
        drive_detail_loader: Callable[[str], dict] | None = None,
        playback_history_loader: Callable[[str], object | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
        playback_parser_service=None,
        preferred_parse_key_loader: Callable[[], str] | None = None,
        danmaku_service=None,
    ) -> None:
        self._spider = spider
        self._plugin_name = plugin_name
        self.supports_search = search_enabled
        self._drive_detail_loader = drive_detail_loader
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver
        self._playback_parser_service = playback_parser_service
        self._preferred_parse_key_loader = preferred_parse_key_loader
        self._danmaku_service = danmaku_service
        self._danmaku_enabled = bool(getattr(self._spider, "danmaku", lambda: False)())
        self._home_loaded = False
        self._home_categories: list[DoubanCategory] = []
        self._home_items: list[VodItem] = []
```

- [ ] **Step 2: Make `_maybe_resolve_danmaku()` ignore `payload["danmu"]` entirely**

Remove the `payload` dependency from `_maybe_resolve_danmaku()` and gate on `_danmaku_enabled` instead:

```python
def _maybe_resolve_danmaku(self, item: PlayItem, url: str) -> None:
    if not self._danmaku_enabled or self._danmaku_service is None:
        return
    search_name = " ".join(part for part in (item.media_title.strip(), item.title.strip()) if part).strip()
    if not search_name:
        return
    reg_src = str(item.vod_id or url or "").strip()
    try:
        candidates = self._danmaku_service.search_danmu(search_name, reg_src)
        if not candidates:
            return
        item.danmaku_xml = self._danmaku_service.resolve_danmu(candidates[0].url)
        logger.info(
            "Spider plugin resolved danmaku plugin=%s source=%s candidate=%s",
            self._plugin_name,
            item.vod_id,
            candidates[0].url,
        )
    except Exception as exc:
        logger.warning(
            "Spider plugin danmaku resolution failed plugin=%s source=%s error=%s",
            self._plugin_name,
            item.vod_id,
            exc,
        )
```

- [ ] **Step 3: Update both playback call sites to match the new helper signature**

Adjust the parse and direct-playback branches so they pass only `item` and the original page or media URL:

```python
if parse_required:
    if self._playback_parser_service is None:
        raise ValueError("当前插件未配置内置解析")
    result = self._playback_parser_service.resolve(
        item.play_source,
        url,
        preferred_key="" if self._preferred_parse_key_loader is None else self._preferred_parse_key_loader(),
    )
    item.url = result.url
    item.headers = dict(result.headers)
    self._maybe_resolve_danmaku(item, url)
    logger.info(
        "Spider plugin resolved parse playback plugin=%s source=%s parser=%s",
        self._plugin_name,
        item.vod_id,
        result.parser_key,
    )
    return None

if not _looks_like_media_url(url):
    raise ValueError("插件未返回可播放地址")
item.url = url
item.headers = _normalize_headers(payload.get("header"))
self._maybe_resolve_danmaku(item, url)
logger.info(
    "Spider plugin resolved playback url plugin=%s source=%s play_source=%s",
    self._plugin_name,
    item.vod_id,
    item.play_source,
)
return None
```

- [ ] **Step 4: Run the targeted danmaku tests and verify they pass**

Run: `uv run pytest tests/test_spider_plugin_controller.py -k 'danmaku' -v`

Expected: PASS for the three danmaku tests:
- `test_controller_resolves_danmaku_when_spider_enables_plugin_level_danmaku`
- `test_controller_ignores_legacy_player_content_danmu_flag_when_plugin_level_danmaku_is_disabled`
- `test_controller_ignores_danmaku_resolution_failures_without_breaking_playback`

- [ ] **Step 5: Commit the implementation**

```bash
git add src/atv_player/plugins/controller.py tests/test_spider_plugin_controller.py
git commit -m "feat: use spider danmaku capability for plugin playback"
```

### Task 3: Run Focused Regression Coverage For The Spider Playback Path

**Files:**
- Modify: none
- Test: `tests/test_spider_plugin_controller.py`
- Test: `tests/test_spider_plugin_manager.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Run the full spider-controller test file**

Run: `uv run pytest tests/test_spider_plugin_controller.py -v`

Expected: PASS with all controller tests green, confirming the danmaku trigger change did not regress drive links, parser-based playback, or grouped playlists.

- [ ] **Step 2: Run plugin-manager coverage to verify controller wiring still works**

Run: `uv run pytest tests/test_spider_plugin_manager.py -v`

Expected: PASS. No manager code changed, but this confirms `SpiderPluginController` still integrates cleanly through `SpiderPluginManager.load_enabled_plugins()`.

- [ ] **Step 3: Run the existing player danmaku UI smoke coverage**

Run: `uv run pytest tests/test_player_window_ui.py -k 'danmaku' -v`

Expected: PASS. The UI should remain unaffected because it only consumes `PlayItem.danmaku_xml`, not the plugin trigger source.

- [ ] **Step 4: Record the result in a final verification commit if needed**

No code changes are expected here. If the worktree is clean after tests, do not create an extra commit. If you had to adjust a test name or assertion during verification, commit only that delta:

```bash
git status --short
```

Expected: no output.

- [ ] **Step 5: Prepare the branch for integration**

```bash
git log --oneline --decorate -3
```

Expected: the latest commits should include:
- `feat: use spider danmaku capability for plugin playback`
- `test: define plugin-level danmaku toggle behavior`

## Self-Review

- Spec coverage: the plan covers plugin-level capability detection, complete removal of `payload["danmu"]` from controller gating, non-breaking failure handling, and explicit regression coverage.
- Placeholder scan: no `TODO`, `TBD`, or implicit “write tests later” steps remain.
- Type consistency: the plan consistently uses `_danmaku_enabled`, `_maybe_resolve_danmaku(self, item, url)`, and `Spider.danmaku()`.
