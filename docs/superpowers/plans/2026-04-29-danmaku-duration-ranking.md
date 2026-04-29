# Danmaku Duration Ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional media-duration-aware danmaku candidate ranking so the full candidate list and default source prefer results closest to the current media duration.

**Architecture:** Keep provider search behavior unchanged and add duration-aware reranking on grouped danmaku source results in `DanmakuService`. Feed target duration from `PlayItem` metadata by default, allow player-window refresh to override it with runtime duration, and rerank cached grouped results in memory so cache keys remain unchanged.

**Tech Stack:** Python, pytest, dataclasses, existing danmaku service/controller/player-window stack

---

## File Structure

- Modify `src/atv_player/danmaku/service.py`
  - add optional `media_duration_seconds` support to `search_danmu_sources(...)`
  - add grouped-result reranking helpers reusable for fresh and cached results
- Modify `src/atv_player/models.py`
  - add `duration_seconds` to `PlayItem`
- Modify `src/atv_player/plugins/controller.py`
  - pass metadata duration into service
  - rerank cached grouped results without changing cache storage format
  - accept optional runtime duration override during manual refresh
- Modify `src/atv_player/ui/player_window.py`
  - pass current playback duration into manual danmaku-source refresh actions
- Modify `tests/test_danmaku_service.py`
  - cover duration-aware source ordering and unknown-duration fallback
- Modify `tests/test_spider_plugin_controller.py`
  - cover controller pass-through and cached-result reranking
- Modify `tests/test_player_window_ui.py`
  - cover runtime player duration pass-through during manual refresh/reset

### Task 1: Add failing service tests for duration-aware source ordering

**Files:**
- Modify: `tests/test_danmaku_service.py`
- Test: `tests/test_danmaku_service.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_search_danmu_sources_reorders_candidates_by_media_duration() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent",
                name="遮天 88集",
                url="https://v.qq.com/x/cover/ep88-long.html",
                ratio=0.98,
                simi=0.98,
                duration_seconds=1560,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="遮天 88集",
                url="https://v.qq.com/x/cover/ep88-best.html",
                ratio=0.96,
                simi=0.96,
                duration_seconds=1242,
            ),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    result = service.search_danmu_sources("遮天 88集", media_duration_seconds=1240)

    assert [option.url for option in result.groups[0].options] == [
        "https://v.qq.com/x/cover/ep88-best.html",
        "https://v.qq.com/x/cover/ep88-long.html",
    ]
    assert result.default_option_url == "https://v.qq.com/x/cover/ep88-best.html"


def test_search_danmu_sources_preserves_existing_order_when_media_duration_unknown() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(provider="tencent", name="遮天 88集", url="https://v.qq.com/x/cover/first.html", ratio=0.98, simi=0.98, duration_seconds=1560),
            DanmakuSearchItem(provider="tencent", name="遮天 88集", url="https://v.qq.com/x/cover/second.html", ratio=0.96, simi=0.96, duration_seconds=1242),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    result = service.search_danmu_sources("遮天 88集", media_duration_seconds=0)

    assert [option.url for option in result.groups[0].options] == [
        "https://v.qq.com/x/cover/first.html",
        "https://v.qq.com/x/cover/second.html",
    ]
```

- [ ] **Step 2: Run the targeted service tests to verify they fail**

Run:

```bash
uv run pytest -q tests/test_danmaku_service.py -k media_duration
```

Expected: FAIL because `search_danmu_sources(...)` does not yet accept `media_duration_seconds` and does not reorder grouped candidates.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_danmaku_service.py
git commit -m "test: cover danmaku duration-aware source ranking"
```

### Task 2: Implement duration-aware reranking in `DanmakuService`

**Files:**
- Modify: `src/atv_player/danmaku/service.py`
- Test: `tests/test_danmaku_service.py`

- [ ] **Step 1: Add the minimal service implementation**

```python
def search_danmu_sources(
    self,
    name: str,
    reg_src: str = "",
    preferred_provider: str = "",
    preferred_page_url: str = "",
    media_duration_seconds: int = 0,
) -> DanmakuSourceSearchResult:
    flat_results = self.search_danmu(name, reg_src)
    requested_episode = extract_episode_number(normalize_name(name))
    grouped: dict[str, list[DanmakuSourceOption]] = {}
    for item in flat_results:
        grouped.setdefault(item.provider, []).append(
            DanmakuSourceOption(
                provider=item.provider,
                name=item.name,
                url=item.url,
                ratio=item.ratio,
                simi=item.simi,
                duration_seconds=item.duration_seconds,
                episode_match=extract_episode_number(item.name) == requested_episode if requested_episode is not None else False,
                preferred_by_history=item.url == preferred_page_url,
            )
        )
    groups = [
        DanmakuSourceGroup(
            provider=provider,
            provider_label=_PROVIDER_LABELS.get(provider, provider),
            options=options,
            preferred_by_history=provider == preferred_provider,
        )
        for provider, options in grouped.items()
    ]
    result = DanmakuSourceSearchResult(groups=groups)
    reranked = self.rerank_danmaku_source_search_result(
        result,
        reg_src=reg_src,
        preferred_provider=preferred_provider,
        preferred_page_url=preferred_page_url,
        media_duration_seconds=media_duration_seconds,
    )
    return reranked


def rerank_danmaku_source_search_result(
    self,
    result: DanmakuSourceSearchResult,
    *,
    reg_src: str = "",
    preferred_provider: str = "",
    preferred_page_url: str = "",
    media_duration_seconds: int = 0,
) -> DanmakuSourceSearchResult:
    if media_duration_seconds <= 0:
        default_option = self._pick_default_source_option(result.groups, preferred_provider, preferred_page_url, reg_src)
        return DanmakuSourceSearchResult(
            groups=result.groups,
            default_option_url=default_option.url if default_option is not None else "",
            default_provider=default_option.provider if default_option is not None else "",
        )

    ranked_rows: list[tuple[DanmakuSourceGroup, DanmakuSourceOption, int]] = []
    for group_index, group in enumerate(result.groups):
        for option_index, option in enumerate(group.options):
            ranked_rows.append((group, option, group_index * 1000 + option_index))

    ranked_rows.sort(
        key=lambda row: self._danmaku_source_option_sort_key(
            row[1],
            preferred_provider=preferred_provider,
            preferred_page_url=preferred_page_url,
            reg_src=reg_src,
            media_duration_seconds=media_duration_seconds,
            stable_index=row[2],
        )
    )
    return self._group_ranked_source_rows(ranked_rows)
```

- [ ] **Step 2: Add the helper methods used by the reranker**

```python
def _danmaku_source_option_sort_key(
    self,
    option: DanmakuSourceOption,
    *,
    preferred_provider: str,
    preferred_page_url: str,
    reg_src: str,
    media_duration_seconds: int,
    stable_index: int,
) -> tuple[int, int, int, int, float, float, int]:
    duration_known = int(option.duration_seconds > 0 and media_duration_seconds > 0)
    duration_gap = abs(option.duration_seconds - media_duration_seconds) if duration_known else 10**9
    preferred_page = int(bool(preferred_page_url) and option.url == preferred_page_url)
    preferred_provider_match = int(bool(preferred_provider) and option.provider == preferred_provider)
    reg_src_provider_match = int(option.provider == self._preferred_provider_key(reg_src))
    return (
        -preferred_page,
        -preferred_provider_match,
        -reg_src_provider_match,
        -int(option.episode_match),
        -duration_known,
        duration_gap,
        stable_index,
    )


def _group_ranked_source_rows(
    self, ranked_rows: list[tuple[DanmakuSourceGroup, DanmakuSourceOption, int]]
) -> DanmakuSourceSearchResult:
    grouped: dict[str, DanmakuSourceGroup] = {}
    ordered_groups: list[DanmakuSourceGroup] = []
    for source_group, option, _ in ranked_rows:
        target = grouped.get(source_group.provider)
        if target is None:
            target = DanmakuSourceGroup(
                provider=source_group.provider,
                provider_label=source_group.provider_label,
                preferred_by_history=source_group.preferred_by_history,
                options=[],
            )
            grouped[source_group.provider] = target
            ordered_groups.append(target)
        target.options.append(option)
    default_option = ordered_groups[0].options[0] if ordered_groups and ordered_groups[0].options else None
    return DanmakuSourceSearchResult(
        groups=ordered_groups,
        default_option_url=default_option.url if default_option is not None else "",
        default_provider=default_option.provider if default_option is not None else "",
    )
```

- [ ] **Step 3: Run the targeted service tests to verify they pass**

Run:

```bash
uv run pytest -q tests/test_danmaku_service.py -k media_duration
```

Expected: PASS for the new duration-ranking tests.

- [ ] **Step 4: Run the broader danmaku service tests**

Run:

```bash
uv run pytest -q tests/test_danmaku_service.py
```

Expected: PASS with the new tests and no regressions in existing search ordering.

- [ ] **Step 5: Commit the service implementation**

```bash
git add src/atv_player/danmaku/service.py tests/test_danmaku_service.py
git commit -m "feat: rank danmaku sources by media duration"
```

### Task 3: Pass duration through `PlayItem` and controller, including cached results

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/plugins/controller.py`
- Modify: `tests/test_spider_plugin_controller.py`
- Test: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Write the failing controller tests**

```python
def test_controller_passes_playitem_duration_to_search_danmu_sources() -> None:
    calls: list[int] = []

    class FakeDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            calls.append(media_duration_seconds)
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="红果短剧", duration_seconds=1240)

    controller.refresh_danmaku_sources(item, force_refresh=True)

    assert calls == [1240]


def test_controller_reranks_cached_danmaku_source_results_by_media_duration(monkeypatch) -> None:
    cached_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[
                    DanmakuSourceOption(provider="tencent", name="遮天 88集", url="https://v.qq.com/long", duration_seconds=1560),
                    DanmakuSourceOption(provider="tencent", name="遮天 88集", url="https://v.qq.com/best", duration_seconds=1242),
                ],
            )
        ],
        default_option_url="https://v.qq.com/long",
        default_provider="tencent",
    )

    class FakeDanmakuService:
        def rerank_danmaku_source_search_result(self, result, **kwargs):
            assert kwargs["media_duration_seconds"] == 1240
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[
                            DanmakuSourceOption(provider="tencent", name="遮天 88集", url="https://v.qq.com/best", duration_seconds=1242),
                            DanmakuSourceOption(provider="tencent", name="遮天 88集", url="https://v.qq.com/long", duration_seconds=1560),
                        ],
                    )
                ],
                default_option_url="https://v.qq.com/best",
                default_provider="tencent",
            )

    monkeypatch.setattr(controller_module, "load_cached_danmaku_source_search_result", lambda name, reg_src: cached_result)
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="红果短剧", duration_seconds=1240)

    controller.refresh_danmaku_sources(item)

    assert item.selected_danmaku_url == "https://v.qq.com/best"
```

- [ ] **Step 2: Run the targeted controller tests to verify they fail**

Run:

```bash
uv run pytest -q tests/test_spider_plugin_controller.py -k "duration_to_search or reranks_cached"
```

Expected: FAIL because `PlayItem` does not yet expose `duration_seconds`, controller does not pass it through, and cached results are applied without reranking.

- [ ] **Step 3: Implement the minimal controller and model changes**

```python
@dataclass(slots=True)
class PlayItem:
    title: str
    url: str
    path: str = ""
    index: int = 0
    size: int = 0
    duration_seconds: int = 0
    vod_id: str = ""
```

```python
def _populate_danmaku_candidates(
    self,
    item: PlayItem,
    query_name: str,
    reg_src: str,
    force_refresh: bool = False,
    media_duration_seconds: int = 0,
) -> str:
    target_duration = media_duration_seconds if media_duration_seconds > 0 else int(getattr(item, "duration_seconds", 0) or 0)
    series_key = build_danmaku_series_key(item.media_title or query_name)
    item.danmaku_series_key = series_key
    item.danmaku_search_query = query_name
    if not force_refresh and self.load_cached_danmaku_sources(item, media_duration_seconds=target_duration):
        return item.selected_danmaku_url
    preference = self._danmaku_preference_store.load(series_key) if self._danmaku_preference_store is not None else None
    result = self._danmaku_service.search_danmu_sources(
        query_name,
        reg_src,
        preferred_provider=preference.provider if preference is not None else "",
        preferred_page_url=preference.page_url if preference is not None else "",
        media_duration_seconds=target_duration,
    )
```

```python
def load_cached_danmaku_sources(
    self,
    item: PlayItem,
    playlist: list[PlayItem] | None = None,
    media_duration_seconds: int = 0,
) -> bool:
    ...
    cached_result = load_cached_danmaku_source_search_result(query_name, reg_src)
    if cached_result is None:
        return False
    target_duration = media_duration_seconds if media_duration_seconds > 0 else int(getattr(item, "duration_seconds", 0) or 0)
    if hasattr(self._danmaku_service, "rerank_danmaku_source_search_result"):
        cached_result = self._danmaku_service.rerank_danmaku_source_search_result(
            cached_result,
            reg_src=reg_src,
            media_duration_seconds=target_duration,
        )
    self._apply_danmaku_source_search_result(item, cached_result)
    return True
```

- [ ] **Step 4: Run the targeted controller tests to verify they pass**

Run:

```bash
uv run pytest -q tests/test_spider_plugin_controller.py -k "duration_to_search or reranks_cached"
```

Expected: PASS for both new controller behaviors.

- [ ] **Step 5: Run the full controller danmaku test file**

Run:

```bash
uv run pytest -q tests/test_spider_plugin_controller.py
```

Expected: PASS with no regressions in source search, cache use, and manual refresh paths.

- [ ] **Step 6: Commit the controller/model changes**

```bash
git add src/atv_player/models.py src/atv_player/plugins/controller.py tests/test_spider_plugin_controller.py
git commit -m "feat: pass media duration into danmaku source search"
```

### Task 4: Use runtime player duration for manual source refresh actions

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing UI tests**

```python
def test_rerun_current_item_danmaku_search_passes_runtime_duration(qtbot: QtBot) -> None:
    calls: list[int] = []

    class FakeDanmakuController:
        def refresh_danmaku_sources(
            self,
            item,
            query_override: str | None = None,
            playlist=None,
            force_refresh: bool = False,
            media_duration_seconds: int = 0,
        ) -> None:
            calls.append(media_duration_seconds)

    class FakeVideo:
        def duration_seconds(self) -> int:
            return 1240

    window = build_test_window(qtbot, danmaku_controller=FakeDanmakuController(), video=FakeVideo())
    window._danmaku_source_query_edit.setText("遮天 88集")

    window._rerun_current_item_danmaku_search()

    assert calls == [1240]
```

- [ ] **Step 2: Run the targeted UI tests to verify they fail**

Run:

```bash
uv run pytest -q tests/test_player_window_ui.py -k runtime_duration
```

Expected: FAIL because the player window does not currently pass runtime duration into `refresh_danmaku_sources(...)`.

- [ ] **Step 3: Implement the player-window pass-through**

```python
def _current_media_duration_seconds(self) -> int:
    if hasattr(self.video, "duration_seconds"):
        try:
            return max(0, int(self.video.duration_seconds() or 0))
        except Exception:
            return 0
    return 0


def _rerun_current_item_danmaku_search(self) -> None:
    ...
    runtime_duration = self._current_media_duration_seconds()
    self._start_danmaku_source_task(
        current_item,
        error_prefix="弹幕源重新搜索失败",
        task=lambda: self.session.danmaku_controller.refresh_danmaku_sources(
            current_item,
            query_override=query,
            force_refresh=True,
            media_duration_seconds=runtime_duration,
        ),
    )
```

- [ ] **Step 4: Run the targeted UI tests to verify they pass**

Run:

```bash
uv run pytest -q tests/test_player_window_ui.py -k runtime_duration
```

Expected: PASS for the new manual-refresh duration test.

- [ ] **Step 5: Run the focused danmaku regression suite**

Run:

```bash
uv run pytest -q tests/test_danmaku_service.py tests/test_spider_plugin_controller.py tests/test_player_window_ui.py
```

Expected: PASS with the new duration-aware ranking behavior and no regressions in danmaku source UI flows.

- [ ] **Step 6: Commit the UI changes**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: use runtime duration for danmaku source refresh"
```

