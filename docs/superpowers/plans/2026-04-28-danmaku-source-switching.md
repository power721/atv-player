# Danmaku Source Switching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add grouped danmaku source selection, series-level source memory, and per-episode temporary search override to desktop playback without changing the existing single-source danmaku resolution contract.

**Architecture:** Keep `resolve_danmu(page_url)` unchanged and add a new grouped search layer in `DanmakuService`. Persist series-level danmaku source memory in a dedicated JSON-backed repository, let `SpiderPluginController` populate `PlayItem` danmaku candidate state, and let `PlayerWindow` expose that state through one shared dialog that can be opened from both a new toolbar button and a video right-click context-menu action.

**Tech Stack:** Python 3, PySide6, pytest, existing danmaku service/controller/player-window architecture, JSON persistence in app data directory.

---

## File Map

- `src/atv_player/danmaku/models.py`
  Add grouped source dataclasses and a series-preference dataclass.
- `src/atv_player/danmaku/preferences.py`
  Add JSON-backed persistence for per-series danmaku source preference.
- `src/atv_player/danmaku/__init__.py`
  Export the new grouped-source and preference helpers.
- `src/atv_player/danmaku/service.py`
  Add grouped search, provider labels, default-selection logic, and series-key normalization helper.
- `src/atv_player/models.py`
  Extend `PlayItem` with danmaku source selection and temporary search state.
- `src/atv_player/plugins/controller.py`
  Populate `PlayItem` danmaku candidate state, use series-level preference during auto-load, and expose controller helpers for manual source switching and temporary re-search.
- `src/atv_player/ui/player_window.py`
  Add the danmaku-source toolbar button, right-click menu action, dialog, and player-side actions for switch/research/reset.
- `src/atv_player/icons/danmaku.svg`
  Add the new toolbar icon.
- `tests/test_danmaku_preferences.py`
  Cover JSON preference persistence and update behavior.
- `tests/test_danmaku_service.py`
  Cover grouped search results and default-selection behavior.
- `tests/test_spider_plugin_controller.py`
  Cover controller population of grouped state, series-preference fallback, manual switch, and per-item query override.
- `tests/test_player_window_ui.py`
  Cover dialog availability, candidate rendering, temporary re-search/reset, and successful manual switch behavior.

### Task 1: Add grouped danmaku models and series preference storage

**Files:**
- Create: `src/atv_player/danmaku/preferences.py`
- Create: `tests/test_danmaku_preferences.py`
- Modify: `src/atv_player/danmaku/models.py`
- Modify: `src/atv_player/danmaku/__init__.py`

- [ ] **Step 1: Write the failing preference-storage tests**

```python
from pathlib import Path

from atv_player.danmaku.models import DanmakuSeriesPreference
from atv_player.danmaku.preferences import DanmakuSeriesPreferenceStore


def test_preference_store_round_trip(tmp_path: Path) -> None:
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    pref = DanmakuSeriesPreference(
        series_key="jianlai",
        provider="tencent",
        page_url="https://v.qq.com/x/cover/demo.html",
        title="剑来 第12集",
        updated_at=1770000000,
    )

    store.save(pref)

    loaded = store.load("jianlai")

    assert loaded == pref


def test_preference_store_overwrites_existing_series_key(tmp_path: Path) -> None:
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    store.save(
        DanmakuSeriesPreference(
            series_key="jianlai",
            provider="youku",
            page_url="https://v.youku.com/v_show/id_old.html",
            title="旧结果",
            updated_at=1,
        )
    )

    store.save(
        DanmakuSeriesPreference(
            series_key="jianlai",
            provider="tencent",
            page_url="https://v.qq.com/x/cover/demo.html",
            title="新结果",
            updated_at=2,
        )
    )

    loaded = store.load("jianlai")

    assert loaded is not None
    assert loaded.provider == "tencent"
    assert loaded.page_url.endswith("demo.html")
    assert store.load("missing") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_danmaku_preferences.py -v`
Expected: FAIL with `ModuleNotFoundError` or missing `DanmakuSeriesPreferenceStore` / `DanmakuSeriesPreference`.

- [ ] **Step 3: Add the new danmaku dataclasses and JSON store**

```python
# src/atv_player/danmaku/models.py
@dataclass(frozen=True, slots=True)
class DanmakuSourceOption:
    provider: str
    name: str
    url: str
    ratio: float = 0.0
    simi: float = 0.0
    duration_seconds: int = 0
    episode_match: bool = False
    preferred_by_history: bool = False
    resolve_ready: bool = True


@dataclass(frozen=True, slots=True)
class DanmakuSourceGroup:
    provider: str
    provider_label: str
    options: list[DanmakuSourceOption]
    preferred_by_history: bool = False


@dataclass(frozen=True, slots=True)
class DanmakuSourceSearchResult:
    groups: list[DanmakuSourceGroup]
    default_option_url: str = ""
    default_provider: str = ""


@dataclass(frozen=True, slots=True)
class DanmakuSeriesPreference:
    series_key: str
    provider: str
    page_url: str
    title: str
    updated_at: int
```

```python
# src/atv_player/danmaku/preferences.py
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from atv_player.danmaku.models import DanmakuSeriesPreference
from atv_player.paths import app_data_dir


def danmaku_series_preference_path() -> Path:
    path = app_data_dir() / "danmaku-series-preferences.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class DanmakuSeriesPreferenceStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else danmaku_series_preference_path()

    def load(self, series_key: str) -> DanmakuSeriesPreference | None:
        payload = self._read_all()
        raw = payload.get(series_key)
        if not isinstance(raw, dict):
            return None
        return DanmakuSeriesPreference(series_key=series_key, **raw)

    def save(self, preference: DanmakuSeriesPreference) -> DanmakuSeriesPreference:
        payload = self._read_all()
        payload[preference.series_key] = {
            "provider": preference.provider,
            "page_url": preference.page_url,
            "title": preference.title,
            "updated_at": preference.updated_at or int(time.time()),
        }
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return preference

    def _read_all(self) -> dict[str, dict[str, object]]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}
```

- [ ] **Step 4: Export the new helpers**

```python
# src/atv_player/danmaku/__init__.py
from atv_player.danmaku.models import (
    DanmakuRecord,
    DanmakuSearchItem,
    DanmakuSeriesPreference,
    DanmakuSourceGroup,
    DanmakuSourceOption,
    DanmakuSourceSearchResult,
)
from atv_player.danmaku.preferences import DanmakuSeriesPreferenceStore, danmaku_series_preference_path
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_danmaku_preferences.py -v`
Expected: PASS with 2 passing tests.

- [ ] **Step 6: Commit**

```bash
git add src/atv_player/danmaku/models.py src/atv_player/danmaku/preferences.py src/atv_player/danmaku/__init__.py tests/test_danmaku_preferences.py
git commit -m "feat: add danmaku series preference storage"
```

### Task 2: Add grouped danmaku search and default-selection logic

**Files:**
- Modify: `src/atv_player/danmaku/service.py`
- Modify: `src/atv_player/danmaku/models.py`
- Test: `tests/test_danmaku_service.py`

- [ ] **Step 1: Write the failing grouped-search service tests**

```python
def test_search_danmu_sources_groups_results_by_provider_and_marks_default() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第12集", url="https://v.qq.com/12", ratio=0.9, simi=0.9)],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="剑来 第12集", url="https://v.youku.com/12", ratio=0.8, simi=0.8)],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    result = service.search_danmu_sources("剑来 第12集")

    assert [group.provider for group in result.groups] == ["tencent", "youku"]
    assert result.default_provider == "tencent"
    assert result.default_option_url == "https://v.qq.com/12"


def test_search_danmu_sources_prefers_exact_historical_page_url() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(provider="tencent", name="剑来 第12集", url="https://v.qq.com/11", ratio=0.95, simi=0.95),
            DanmakuSearchItem(provider="tencent", name="剑来 第12集", url="https://v.qq.com/12", ratio=0.80, simi=0.80),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    result = service.search_danmu_sources(
        "剑来 第12集",
        preferred_provider="tencent",
        preferred_page_url="https://v.qq.com/12",
    )

    assert result.default_option_url == "https://v.qq.com/12"
    assert result.groups[0].options[1].preferred_by_history is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_danmaku_service.py::test_search_danmu_sources_groups_results_by_provider_and_marks_default tests/test_danmaku_service.py::test_search_danmu_sources_prefers_exact_historical_page_url -v`
Expected: FAIL because `search_danmu_sources()` does not exist.

- [ ] **Step 3: Add provider labels, series-key helper, grouped result builder, and default-selection logic**

```python
# src/atv_player/danmaku/service.py
_PROVIDER_LABELS = {
    "tencent": "腾讯",
    "youku": "优酷",
    "bilibili": "B站",
    "iqiyi": "爱奇艺",
    "mgtv": "芒果",
}


def build_danmaku_series_key(name: str) -> str:
    normalized = normalize_name(strip_episode_suffix(name))
    return _compact_title(normalized)


def search_danmu_sources(
    self,
    name: str,
    reg_src: str = "",
    preferred_provider: str = "",
    preferred_page_url: str = "",
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
    default_option = self._pick_default_source_option(groups, preferred_provider, preferred_page_url, reg_src)
    return DanmakuSourceSearchResult(
        groups=groups,
        default_option_url=default_option.url if default_option is not None else "",
        default_provider=default_option.provider if default_option is not None else "",
    )
```

- [ ] **Step 4: Add the default-selection helper**

```python
def _pick_default_source_option(
    self,
    groups: list[DanmakuSourceGroup],
    preferred_provider: str,
    preferred_page_url: str,
    reg_src: str,
) -> DanmakuSourceOption | None:
    for group in groups:
        for option in group.options:
            if preferred_page_url and option.url == preferred_page_url:
                return option
    if preferred_provider:
        for group in groups:
            if group.provider == preferred_provider and group.options:
                return group.options[0]
    matched_provider = self._preferred_provider_key(reg_src)
    if matched_provider:
        for group in groups:
            if group.provider == matched_provider and group.options:
                return group.options[0]
    for group in groups:
        if group.options:
            return group.options[0]
    return None
```

- [ ] **Step 5: Run the service tests**

Run: `uv run pytest tests/test_danmaku_service.py -v`
Expected: PASS including the new grouped-search tests and existing danmaku service tests.

- [ ] **Step 6: Commit**

```bash
git add src/atv_player/danmaku/service.py src/atv_player/danmaku/models.py tests/test_danmaku_service.py
git commit -m "feat: group danmaku search results by provider"
```

### Task 3: Populate PlayItem danmaku source state in the plugin controller

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/plugins/controller.py`
- Test: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Write the failing controller tests**

```python
def test_controller_populates_grouped_danmaku_candidates_on_successful_search() -> None:
    class FakeDanmakuService:
        def search_danmu_sources(self, name: str, reg_src: str = "", preferred_provider: str = "", preferred_page_url: str = ""):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>'

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    request = controller.build_request("/detail/1")
    item = request.playlist[0]
    request.playback_loader(item)
    _wait_until(lambda: item.danmaku_xml != "")

    assert item.selected_danmaku_provider == "tencent"
    assert item.selected_danmaku_url == "https://v.qq.com/demo"
    assert item.danmaku_search_query == "红果短剧 1集"
    assert len(item.danmaku_candidates) == 1


def test_controller_research_danmaku_uses_temporary_query_only_for_current_item() -> None:
    calls: list[str] = []

    class FakeDanmakuService:
        def search_danmu_sources(self, name: str, reg_src: str = "", preferred_provider: str = "", preferred_page_url: str = ""):
            calls.append(name)
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="红果短剧")

    controller.refresh_danmaku_sources(item, query_override="红果短剧 腾讯版")

    assert item.danmaku_search_query == "红果短剧 腾讯版"
    assert item.danmaku_search_query_overridden is True
    assert calls[-1] == "红果短剧 腾讯版"
```

- [ ] **Step 2: Run the controller tests to verify they fail**

Run: `uv run pytest tests/test_spider_plugin_controller.py::test_controller_populates_grouped_danmaku_candidates_on_successful_search tests/test_spider_plugin_controller.py::test_controller_research_danmaku_uses_temporary_query_only_for_current_item -v`
Expected: FAIL because `PlayItem` lacks the new fields and the controller does not expose grouped-search helpers.

- [ ] **Step 3: Extend `PlayItem` with danmaku source state**

```python
# src/atv_player/models.py
danmaku_series_key: str = ""
danmaku_search_query: str = ""
danmaku_search_query_overridden: bool = False
danmaku_candidates: list[DanmakuSourceGroup] = field(default_factory=list)
selected_danmaku_url: str = ""
selected_danmaku_provider: str = ""
selected_danmaku_title: str = ""
danmaku_error: str = ""
```

- [ ] **Step 4: Inject preference store and grouped search into the controller**

```python
# src/atv_player/plugins/controller.py
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
    danmaku_preference_store=None,
) -> None:
    self._danmaku_service = danmaku_service
    self._danmaku_preference_store = danmaku_preference_store


def _populate_danmaku_candidates(self, item: PlayItem, query_name: str, reg_src: str) -> str:
    series_key = build_danmaku_series_key(item.media_title or query_name)
    item.danmaku_series_key = series_key
    item.danmaku_search_query = query_name
    preference = self._danmaku_preference_store.load(series_key) if self._danmaku_preference_store is not None else None
    result = self._danmaku_service.search_danmu_sources(
        query_name,
        reg_src,
        preferred_provider=preference.provider if preference is not None else "",
        preferred_page_url=preference.page_url if preference is not None else "",
    )
    item.danmaku_candidates = result.groups
    item.selected_danmaku_provider = result.default_provider
    item.selected_danmaku_url = result.default_option_url
    item.selected_danmaku_title = self._lookup_selected_danmaku_title(result.groups, result.default_option_url)
    item.danmaku_error = ""
    return result.default_option_url
```

- [ ] **Step 5: Add controller helpers for temporary re-search and manual switch**

```python
def refresh_danmaku_sources(self, item: PlayItem, query_override: str | None = None) -> None:
    query_name = (query_override or _build_danmaku_search_name(item)).strip()
    item.danmaku_search_query = query_name
    item.danmaku_search_query_overridden = query_override is not None
    reg_src = str(item.vod_id or item.url or "").strip()
    self._populate_danmaku_candidates(item, query_name, reg_src)


def switch_danmaku_source(self, item: PlayItem, page_url: str) -> str:
    xml_text = self._danmaku_service.resolve_danmu(page_url)
    item.danmaku_xml = xml_text
    item.selected_danmaku_url = page_url
    item.selected_danmaku_title = self._lookup_selected_danmaku_title(item.danmaku_candidates, page_url)
    if self._danmaku_preference_store is not None and item.danmaku_series_key:
        self._danmaku_preference_store.save(
            DanmakuSeriesPreference(
                series_key=item.danmaku_series_key,
                provider=item.selected_danmaku_provider,
                page_url=page_url,
                title=item.selected_danmaku_title,
                updated_at=int(time.time()),
            )
        )
    return xml_text
```

- [ ] **Step 6: Rework `_resolve_danmaku_sync()` to use grouped state and fallback**

```python
default_url = self._populate_danmaku_candidates(item, search_name, reg_src)
for candidate in self._iter_danmaku_candidate_urls(item.danmaku_candidates, default_url):
    try:
        item.selected_danmaku_provider = candidate.provider
        item.selected_danmaku_url = candidate.url
        item.selected_danmaku_title = candidate.name
        item.danmaku_xml = self._danmaku_service.resolve_danmu(candidate.url)
        save_cached_danmaku_xml(search_name, reg_src, item.danmaku_xml)
        return
    except Exception as exc:
        item.danmaku_error = str(exc)
```

- [ ] **Step 7: Run the controller test file**

Run: `uv run pytest tests/test_spider_plugin_controller.py -v`
Expected: PASS including the new grouped-state and temporary-query tests.

- [ ] **Step 8: Commit**

```bash
git add src/atv_player/models.py src/atv_player/plugins/controller.py tests/test_spider_plugin_controller.py
git commit -m "feat: expose danmaku source state in plugin playback"
```

### Task 4: Add the player dialog, toolbar button, and temporary re-search UI

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Create: `src/atv_player/icons/danmaku.svg`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing player-window tests**

```python
def test_player_window_shows_danmaku_source_button_with_custom_icon(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.danmaku_source_button.toolTip() == "弹幕源"
    assert window.danmaku_source_button.isEnabled() is False


def test_player_window_video_context_menu_contains_danmaku_source_action_when_candidates_exist(qtbot) -> None:
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/demo")],
            )
        ],
    )
    session = PlayerSession(vod=VodItem(vod_id="1", vod_name="红果短剧"), playlist=[item], start_index=0, start_position_seconds=0, speed=1.0)
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    menu = window._build_video_context_menu()

    assert any(action.text() == "弹幕源" for action in menu.actions())


def test_player_window_opens_danmaku_source_dialog_for_current_item(qtbot) -> None:
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/demo")],
            )
        ],
        selected_danmaku_provider="tencent",
        selected_danmaku_url="https://v.qq.com/demo",
        selected_danmaku_title="红果短剧 第1集",
        danmaku_search_query="红果短剧 1集",
    )
    session = PlayerSession(vod=VodItem(vod_id="1", vod_name="红果短剧"), playlist=[item], start_index=0, start_position_seconds=0, speed=1.0)
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    window._open_danmaku_source_dialog()

    assert window._danmaku_source_dialog is not None
    assert window._danmaku_source_query_edit.text() == "红果短剧 1集"
    assert window._danmaku_source_provider_list.count() == 1
```

- [ ] **Step 2: Run the player-window tests to verify they fail**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_shows_danmaku_source_button_with_custom_icon tests/test_player_window_ui.py::test_player_window_video_context_menu_contains_danmaku_source_action_when_candidates_exist tests/test_player_window_ui.py::test_player_window_opens_danmaku_source_dialog_for_current_item -v`
Expected: FAIL because the player window has no danmaku source button or dialog.

- [ ] **Step 3: Add the toolbar button, context-menu action, and icon asset**

```python
# src/atv_player/ui/player_window.py
self.danmaku_source_button = self._create_icon_button("danmaku.svg", "弹幕源")
self.danmaku_source_button.clicked.connect(self._open_danmaku_source_dialog)
self.danmaku_source_button.setEnabled(False)

danmaku_source_action = menu.addAction("弹幕源")
danmaku_source_action.triggered.connect(self._open_danmaku_source_dialog)
danmaku_source_action.setEnabled(bool(current_item.danmaku_candidates))
```

```svg
<!-- src/atv_player/icons/danmaku.svg -->
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <path d="M5 6.5C5 4.567 6.567 3 8.5 3h7C17.433 3 19 4.567 19 6.5v5C19 13.433 17.433 15 15.5 15H11l-4 4v-4.3C5.854 14.338 5 13.021 5 11.5z"/>
  <path d="M9 8h6"/>
  <path d="M9 11h4"/>
</svg>
```

- [ ] **Step 4: Add the dialog widgets and refresh logic**

```python
def _open_danmaku_source_dialog(self) -> None:
    current_item = self.session.playlist[self.current_index]
    dialog = self._ensure_danmaku_source_dialog()
    self._danmaku_source_query_edit.setText(current_item.danmaku_search_query)
    self._populate_danmaku_source_provider_list(current_item.danmaku_candidates)
    self._populate_danmaku_source_option_list(current_item.danmaku_candidates, current_item.selected_danmaku_provider)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
```

- [ ] **Step 5: Wire temporary re-search, reset, and manual switch actions**

```python
def _rerun_current_item_danmaku_search(self) -> None:
    current_item = self.session.playlist[self.current_index]
    query = self._danmaku_source_query_edit.text().strip()
    self.session.danmaku_controller.refresh_danmaku_sources(current_item, query_override=query)
    self._refresh_danmaku_source_dialog_from_item(current_item)


def _reset_current_item_danmaku_search_query(self) -> None:
    current_item = self.session.playlist[self.current_index]
    self.session.danmaku_controller.refresh_danmaku_sources(current_item, query_override=None)
    self._refresh_danmaku_source_dialog_from_item(current_item)


def _switch_current_item_danmaku_source(self) -> None:
    current_item = self.session.playlist[self.current_index]
    selected_url = self._selected_danmaku_source_url_from_dialog()
    self.session.danmaku_controller.switch_danmaku_source(current_item, selected_url)
    self._configure_danmaku_for_current_item()
    self._refresh_danmaku_source_dialog_from_item(current_item)
```

- [ ] **Step 6: Run the focused player-window tests**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_shows_danmaku_source_button_with_custom_icon tests/test_player_window_ui.py::test_player_window_video_context_menu_contains_danmaku_source_action_when_candidates_exist tests/test_player_window_ui.py::test_player_window_opens_danmaku_source_dialog_for_current_item -v`
Expected: PASS.

- [ ] **Step 7: Add one more player-window test for temporary query reset**

```python
def test_player_window_reset_danmaku_source_query_restores_default(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def refresh_danmaku_sources(self, item: PlayItem, query_override: str | None = None) -> None:
            self.calls.append(query_override)
            item.danmaku_search_query = "红果短剧 1集" if query_override is None else query_override
            item.danmaku_search_query_overridden = query_override is not None

    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_search_query="红果短剧 腾讯版",
        danmaku_search_query_overridden=True,
    )
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=FakeDanmakuController(),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    window._open_danmaku_source_dialog()
    window._reset_current_item_danmaku_search_query()

    assert window._danmaku_source_query_edit.text() == "红果短剧 1集"
    assert item.danmaku_search_query_overridden is False
```

- [ ] **Step 8: Run the full player-window danmaku test subset**

Run: `uv run pytest tests/test_player_window_ui.py -k "danmaku and (source or selection)" -v`
Expected: PASS for the new source-dialog tests plus existing danmaku preference tests.

- [ ] **Step 9: Commit**

```bash
git add src/atv_player/ui/player_window.py src/atv_player/icons/danmaku.svg tests/test_player_window_ui.py
git commit -m "feat: add player danmaku source dialog"
```

### Task 5: Integrate the dialog with the real controller path and run regressions

**Files:**
- Modify: `src/atv_player/controllers/player_controller.py`
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `src/atv_player/app.py`
- Test: `tests/test_spider_plugin_controller.py`
- Test: `tests/test_player_window_ui.py`
- Test: `tests/test_danmaku_service.py`
- Test: `tests/test_danmaku_preferences.py`

- [ ] **Step 1: Write the failing integration test for real window/controller collaboration**

```python
def test_player_window_manual_danmaku_source_switch_reconfigures_current_item(qtbot) -> None:
    class FakeDanmakuController:
        def switch_danmaku_source(self, item: PlayItem, page_url: str) -> str:
            item.selected_danmaku_url = page_url
            item.selected_danmaku_provider = "tencent"
            item.selected_danmaku_title = "红果短剧 第1集"
            item.danmaku_xml = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>'
            return item.danmaku_xml

    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/demo")],
            )
        ],
        selected_danmaku_provider="tencent",
        selected_danmaku_url="https://v.qq.com/demo",
        selected_danmaku_title="红果短剧 第1集",
        danmaku_search_query="红果短剧 1集",
    )
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=FakeDanmakuController(),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    window._open_danmaku_source_dialog()
    window._switch_current_item_danmaku_source()

    assert item.selected_danmaku_url == "https://v.qq.com/demo"
    assert "ok" in item.danmaku_xml
```

- [ ] **Step 2: Run the integration test to verify it fails**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_manual_danmaku_source_switch_reconfigures_current_item -v`
Expected: FAIL because the window cannot yet call back into the real playback/controller path consistently.

- [ ] **Step 3: Pass the controller callbacks through the app/session path**

```python
# src/atv_player/controllers/player_controller.py
@dataclass(slots=True)
class PlayerSession:
    vod: VodItem
    playlist: list[PlayItem]
    start_index: int
    start_position_seconds: int
    speed: float
    opening_seconds: int = 0
    ending_seconds: int = 0
    danmaku_controller: object | None = None
```

```python
# src/atv_player/app.py or request/session construction path
PlayerSession(
    vod=request.vod,
    playlist=request.playlist,
    start_index=request.start_index,
    start_position_seconds=request.start_position_seconds,
    speed=request.speed,
    danmaku_controller=request.danmaku_controller,
)
```

```python
# src/atv_player/ui/player_window.py
if self.session is None or self.session.danmaku_controller is None:
    return
self.session.danmaku_controller.switch_danmaku_source(current_item, selected_url)
```

- [ ] **Step 4: Run the targeted danmaku regression suite**

Run: `uv run pytest tests/test_danmaku_preferences.py tests/test_danmaku_service.py tests/test_spider_plugin_controller.py tests/test_player_window_ui.py -k "danmaku" -v`
Expected: PASS.

- [ ] **Step 5: Run the broader player and storage regression subset**

Run: `uv run pytest tests/test_storage.py tests/test_player_window_ui.py tests/test_spider_plugin_controller.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/atv_player/controllers/player_controller.py src/atv_player/ui/player_window.py src/atv_player/app.py tests/test_player_window_ui.py tests/test_spider_plugin_controller.py
git commit -m "feat: wire danmaku source dialog into playback flow"
```
