# Spider Plugin Category Filters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show plugin-provided category filters in spider-plugin poster pages, keep them collapsed by default, and pass the selected values into `categoryContent(..., extend)` without changing plugin search behavior.

**Architecture:** Add shared typed filter models to `DoubanCategory`, extend poster-grid controller `load_items()` signatures so pages can always pass an optional filter dictionary, then teach `SpiderPluginController` to map `homeContent().filters` onto categories and teach `PosterGridPage` to render and remember per-category filter controls. Search mode remains independent and hides the category filter UI.

**Tech Stack:** Python 3.13, PySide6, pytest, existing poster-grid page/controller patterns

---

## File Structure

- Modify: `src/atv_player/models.py`
  Add typed category filter models and attach them to `DoubanCategory`.
- Modify: `src/atv_player/controllers/douban_controller.py`
  Keep the shared controller signature aligned with poster-grid pages and ignore optional filters.
- Modify: `src/atv_player/controllers/emby_controller.py`
  Accept optional filters and ignore them.
- Modify: `src/atv_player/controllers/jellyfin_controller.py`
  Accept optional filters and ignore them.
- Modify: `src/atv_player/controllers/live_controller.py`
  Accept optional filters and ignore them for both built-in and custom live categories.
- Modify: `src/atv_player/controllers/telegram_search_controller.py`
  Accept optional filters and ignore them.
- Modify: `src/atv_player/plugins/controller.py`
  Parse plugin `filters`, map them to categories, and pass selected values into `categoryContent(..., extend)`.
- Modify: `src/atv_player/ui/poster_grid_page.py`
  Add the collapsible filter UI, per-category remembered selections, and filter-aware category loading while keeping search independent.
- Modify: `tests/test_spider_plugin_controller.py`
  Lock category filter mapping and extend-passing behavior.
- Modify: `tests/test_poster_grid_page_ui.py`
  Lock filter button visibility, collapse/expand behavior, remembered selections, and search-mode hiding.
- Modify: `tests/test_douban_controller.py`
  Lock the optional `filters` argument as a no-op shared controller contract.
- Modify: `tests/test_live_controller.py`
  Lock the optional `filters` argument as a no-op shared controller contract.
- Modify: `tests/test_emby_controller.py`
  Lock the optional `filters` argument as a no-op shared controller contract.
- Modify: `tests/test_jellyfin_controller.py`
  Lock the optional `filters` argument as a no-op shared controller contract.
- Modify: `tests/test_telegram_search_controller.py`
  Lock the optional `filters` argument as a no-op shared controller contract.

### Task 1: Add Shared Category Filter Models And Plugin Controller Wiring

**Files:**
- Modify: `tests/test_spider_plugin_controller.py`
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/plugins/controller.py`

- [ ] **Step 1: Write the failing spider-plugin controller tests**

Add these tests to `tests/test_spider_plugin_controller.py`:

```python
from atv_player.models import CategoryFilter, CategoryFilterOption


class FilterSpider(FakeSpider):
    def __init__(self) -> None:
        self.category_calls: list[tuple[str, str, bool, dict[str, str]]] = []

    def homeContent(self, filter):
        return {
            "class": [
                {"type_id": "movie", "type_name": "电影"},
                {"type_id": "tv", "type_name": "剧集"},
            ],
            "filters": {
                "movie": [
                    {
                        "key": "sc",
                        "name": "影视类型",
                        "value": [
                            {"n": "不限", "v": "0"},
                            {"n": "动作", "v": "6"},
                        ],
                    }
                ],
                "tv": [
                    {
                        "key": "status",
                        "name": "剧集状态",
                        "value": [
                            {"n": "不限", "v": "0"},
                            {"n": "连载中", "v": "1"},
                        ],
                    }
                ],
            },
            "list": [],
        }

    def categoryContent(self, tid, pg, filter, extend):
        self.category_calls.append((tid, pg, filter, dict(extend)))
        return {
            "list": [{"vod_id": f"/detail/{tid}-{pg}", "vod_name": f"{tid}-{pg}"}],
            "total": 1,
        }


def test_controller_maps_home_filters_to_matching_categories() -> None:
    controller = SpiderPluginController(FilterSpider(), plugin_name="筛选插件", search_enabled=True)

    categories = controller.load_categories()

    movie = categories[0]
    tv = categories[1]

    assert movie.type_id == "movie"
    assert movie.filters == [
        CategoryFilter(
            key="sc",
            name="影视类型",
            options=[
                CategoryFilterOption(name="不限", value="0"),
                CategoryFilterOption(name="动作", value="6"),
            ],
        )
    ]
    assert tv.filters[0].key == "status"
    assert [option.name for option in tv.filters[0].options] == ["不限", "连载中"]


def test_controller_passes_selected_filters_into_category_content_extend() -> None:
    spider = FilterSpider()
    controller = SpiderPluginController(spider, plugin_name="筛选插件", search_enabled=True)

    items, total = controller.load_items("movie", 2, filters={"sc": "6"})

    assert total == 1
    assert items[0].vod_name == "movie-2"
    assert spider.category_calls == [("movie", "2", False, {"sc": "6"})]


def test_controller_ignores_filters_for_home_category_items() -> None:
    spider = FilterSpider()
    controller = SpiderPluginController(spider, plugin_name="筛选插件", search_enabled=True)

    controller.load_categories()
    controller.load_items("home", 1, filters={"sc": "6"})

    assert spider.category_calls == []
```

- [ ] **Step 2: Run the focused controller tests to verify they fail**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py::test_controller_maps_home_filters_to_matching_categories tests/test_spider_plugin_controller.py::test_controller_passes_selected_filters_into_category_content_extend tests/test_spider_plugin_controller.py::test_controller_ignores_filters_for_home_category_items -v
```

Expected: FAIL because `DoubanCategory` does not yet expose `filters`, the filter model types do not exist, and `SpiderPluginController.load_items()` does not accept a `filters` argument.

- [ ] **Step 3: Add the shared category filter models**

Update `src/atv_player/models.py`:

```python
@dataclass(slots=True)
class CategoryFilterOption:
    name: str
    value: str


@dataclass(slots=True)
class CategoryFilter:
    key: str
    name: str
    options: list[CategoryFilterOption] = field(default_factory=list)


@dataclass(slots=True)
class DoubanCategory:
    type_id: str
    type_name: str
    filters: list[CategoryFilter] = field(default_factory=list)
```

- [ ] **Step 4: Parse plugin filters and pass them into `categoryContent()`**

Update `src/atv_player/plugins/controller.py`:

```python
from atv_player.models import CategoryFilter, CategoryFilterOption, DoubanCategory, OpenPlayerRequest, PlayItem, PlaybackLoadResult, VodItem


def _map_filter_option(payload: dict) -> CategoryFilterOption | None:
    name = str(payload.get("n") or "").strip()
    value = str(payload.get("v") or "").strip()
    if not name or not value:
        return None
    return CategoryFilterOption(name=name, value=value)


def _map_category_filters(payload) -> list[CategoryFilter]:
    if not isinstance(payload, list):
        return []
    groups: list[CategoryFilter] = []
    for raw_group in payload:
        if not isinstance(raw_group, dict):
            continue
        key = str(raw_group.get("key") or "").strip()
        name = str(raw_group.get("name") or "").strip()
        if not key or not name:
            continue
        options = [
            option
            for option in (_map_filter_option(raw_option) for raw_option in raw_group.get("value") or [])
            if option is not None
        ]
        if not options:
            continue
        groups.append(CategoryFilter(key=key, name=name, options=options))
    return groups


def _ensure_home_loaded(self) -> None:
    if self._home_loaded:
        return
    payload = self._spider.homeContent(False) or {}
    raw_filters = payload.get("filters") or {}
    categories = []
    for item in payload.get("class", []):
        type_id = str(item.get("type_id") or "")
        categories.append(
            DoubanCategory(
                type_id=type_id,
                type_name=str(item.get("type_name") or ""),
                filters=_map_category_filters(raw_filters.get(type_id)),
            )
        )
    items = self._map_items(payload)
    if items:
        categories = [DoubanCategory(type_id="home", type_name="推荐"), *categories]
    self._home_categories = categories
    self._home_items = items
    self._home_loaded = True


def load_items(
    self,
    category_id: str,
    page: int,
    filters: dict[str, str] | None = None,
) -> tuple[list[VodItem], int]:
    self._ensure_home_loaded()
    if category_id == "home":
        return list(self._home_items), len(self._home_items)
    payload = self._spider.categoryContent(category_id, str(page), False, dict(filters or {})) or {}
    items = self._map_items(payload)
    total = int(payload.get("total") or 0)
    if total <= 0:
        total = len(items)
    return items, total
```

- [ ] **Step 5: Re-run the focused controller tests to verify they pass**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py::test_controller_maps_home_filters_to_matching_categories tests/test_spider_plugin_controller.py::test_controller_passes_selected_filters_into_category_content_extend tests/test_spider_plugin_controller.py::test_controller_ignores_filters_for_home_category_items -v
```

Expected: PASS for all three tests.

- [ ] **Step 6: Commit the shared model and plugin-controller slice**

Run:

```bash
git add tests/test_spider_plugin_controller.py src/atv_player/models.py src/atv_player/plugins/controller.py
git commit -m "feat: add spider plugin category filter models"
```

### Task 2: Render Collapsible Category Filters In PosterGridPage

**Files:**
- Modify: `tests/test_poster_grid_page_ui.py`
- Modify: `src/atv_player/ui/poster_grid_page.py`

- [ ] **Step 1: Write the failing poster-grid page tests**

Add these helpers and tests to `tests/test_poster_grid_page_ui.py`:

```python
from atv_player.models import CategoryFilter, CategoryFilterOption, DoubanCategory, VodItem


class FilterablePosterController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self.categories = [
            DoubanCategory(
                type_id="movie",
                type_name="电影",
                filters=[
                    CategoryFilter(
                        key="sc",
                        name="影视类型",
                        options=[
                            CategoryFilterOption(name="不限", value="0"),
                            CategoryFilterOption(name="动作", value="6"),
                        ],
                    )
                ],
            ),
            DoubanCategory(type_id="tv", type_name="剧集"),
        ]
        self.item_calls: list[tuple[str, int, dict[str, str] | None]] = []

    def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
        self.item_calls.append((category_id, page, None if filters is None else dict(filters)))
        return self.items_by_category.get(category_id, ([], 0))


def test_poster_grid_page_hides_filter_button_by_default(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController(), click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.category_list.count() == 2)

    assert page.filter_toggle_button.isHidden() is True
    assert page.filter_panel.isHidden() is True


def test_poster_grid_page_shows_filter_button_for_filtered_category_and_stays_collapsed(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FilterablePosterController(), click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    qtbot.waitUntil(lambda: page.selected_category_id == "movie")

    assert page.filter_toggle_button.isHidden() is False
    assert page.filter_panel.isHidden() is True


def test_poster_grid_page_expands_filters_and_reloads_page_one_on_change(qtbot) -> None:
    controller = FilterablePosterController()
    page = show_loaded_page(qtbot, PosterGridPage(controller, click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.current_page = 3
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    combo = page.filter_combos["sc"]
    combo.setCurrentIndex(combo.findData("6"))

    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("movie", 1, {"sc": "6"}))
    assert page.current_page == 1


def test_poster_grid_page_remembers_filter_state_per_category(qtbot) -> None:
    controller = FilterablePosterController()
    controller.categories = [
        DoubanCategory(
            type_id="movie",
            type_name="电影",
            filters=[
                CategoryFilter(
                    key="sc",
                    name="影视类型",
                    options=[
                        CategoryFilterOption(name="不限", value="0"),
                        CategoryFilterOption(name="动作", value="6"),
                    ],
                )
            ],
        ),
        DoubanCategory(
            type_id="tv",
            type_name="剧集",
            filters=[
                CategoryFilter(
                    key="status",
                    name="剧集状态",
                    options=[
                        CategoryFilterOption(name="不限", value="0"),
                        CategoryFilterOption(name="连载中", value="1"),
                    ],
                )
            ],
        ),
    ]
    page = show_loaded_page(qtbot, PosterGridPage(controller, click_action="open", search_enabled=False))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.filter_toggle_button.click()
    page.filter_combos["sc"].setCurrentIndex(page.filter_combos["sc"].findData("6"))
    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("movie", 1, {"sc": "6"}))

    page.category_list.setCurrentRow(1)
    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("tv", 1, {"status": "0"}))
    page.filter_toggle_button.click()
    page.filter_combos["status"].setCurrentIndex(page.filter_combos["status"].findData("1"))
    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("tv", 1, {"status": "1"}))

    page.category_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.filter_toggle_button.click()

    assert page.filter_combos["sc"].currentData() == "6"


def test_poster_grid_page_hides_category_filters_during_search_and_restores_them_after_clear(qtbot) -> None:
    class SearchableFilterController(FilterablePosterController):
        def __init__(self) -> None:
            super().__init__()
            self.search_calls: list[tuple[str, int]] = []

        def search_items(self, keyword: str, page: int):
            self.search_calls.append((keyword, page))
            return ([VodItem(vod_id="search-1", vod_name="搜索结果")], 1)

    controller = SearchableFilterController()
    page = show_loaded_page(qtbot, PosterGridPage(controller, click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.keyword_edit.setText("黑袍纠察队")
    page.search()

    qtbot.waitUntil(lambda: controller.search_calls == [("黑袍纠察队", 1)])
    assert page.filter_toggle_button.isHidden() is True
    assert page.filter_panel.isHidden() is True

    page.clear_search()

    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("movie", 1, {"sc": "0"}))
    assert page.filter_toggle_button.isHidden() is False
```

- [ ] **Step 2: Run the focused poster-grid tests to verify they fail**

Run:

```bash
uv run pytest tests/test_poster_grid_page_ui.py::test_poster_grid_page_hides_filter_button_by_default tests/test_poster_grid_page_ui.py::test_poster_grid_page_shows_filter_button_for_filtered_category_and_stays_collapsed tests/test_poster_grid_page_ui.py::test_poster_grid_page_expands_filters_and_reloads_page_one_on_change tests/test_poster_grid_page_ui.py::test_poster_grid_page_remembers_filter_state_per_category tests/test_poster_grid_page_ui.py::test_poster_grid_page_hides_category_filters_during_search_and_restores_them_after_clear -v
```

Expected: FAIL because `PosterGridPage` does not yet expose filter controls or pass filter state into `load_items()`.

- [ ] **Step 3: Add the collapsible filter UI and remembered category filter state**

Update `src/atv_player/ui/poster_grid_page.py`:

```python
from PySide6.QtWidgets import QComboBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget


self.filter_toggle_button = QPushButton("筛选")
self.filter_panel = QFrame()
self.filter_panel_layout = QFormLayout(self.filter_panel)
self.filter_panel.hide()
self.filter_toggle_button.hide()
self.filter_combos: dict[str, QComboBox] = {}
self._category_filter_state: dict[str, dict[str, str]] = {}

if self._search_enabled:
    search_row.addWidget(self.filter_toggle_button)
else:
    right.addWidget(self.filter_toggle_button)
right.addWidget(self.filter_panel)
self.filter_toggle_button.clicked.connect(self._toggle_filters)
```

Add these helpers:

```python
def _current_category(self):
    row = self.category_list.currentRow()
    if not (0 <= row < len(self.categories)):
        return None
    return self.categories[row]


def _current_category_filters(self) -> list:
    category = self._current_category()
    if category is None:
        return []
    return list(getattr(category, "filters", []))


def _default_filter_state(self, category) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for group in getattr(category, "filters", []):
        if group.options:
            defaults[group.key] = group.options[0].value
    return defaults


def _selected_filter_values(self) -> dict[str, str]:
    selected: dict[str, str] = {}
    for key, combo in self.filter_combos.items():
        value = combo.currentData()
        if value is not None:
            selected[key] = str(value)
    return selected


def _remember_current_filter_state(self) -> None:
    if not self.selected_category_id:
        return
    self._category_filter_state[self.selected_category_id] = self._selected_filter_values()
```

Render and visibility management:

```python
def _rebuild_filter_panel(self) -> None:
    while self.filter_panel_layout.rowCount():
        self.filter_panel_layout.removeRow(0)
    self.filter_combos = {}
    category = self._current_category()
    if category is None or not category.filters:
        self.filter_panel.hide()
        self.filter_toggle_button.hide()
        return
    state = self._category_filter_state.setdefault(category.type_id, self._default_filter_state(category))
    for group in category.filters:
        combo = QComboBox()
        for option in group.options:
            combo.addItem(option.name, option.value)
        index = combo.findData(state.get(group.key, group.options[0].value))
        combo.setCurrentIndex(max(index, 0))
        combo.currentIndexChanged.connect(self._handle_filter_changed)
        self.filter_panel_layout.addRow(group.name, combo)
        self.filter_combos[group.key] = combo
    self.filter_toggle_button.setVisible(not self._search_mode)
    self.filter_panel.setVisible(False)


def _toggle_filters(self) -> None:
    if not self.filter_combos:
        return
    self.filter_panel.setVisible(not self.filter_panel.isVisible())


def _handle_filter_changed(self) -> None:
    if self._search_mode or not self.selected_category_id:
        return
    self._remember_current_filter_state()
    self.current_page = 1
    self.load_items(self.selected_category_id, self.current_page)
```

Make category and search flows filter-aware:

```python
def load_items(self, category_id: str, page: int) -> None:
    self._items_request_id += 1
    request_id = self._items_request_id
    active_filters = dict(self._category_filter_state.get(category_id, {}))
    self.status_label.setText("加载中...")

    def run() -> None:
        try:
            items, total = self.controller.load_items(category_id, page, filters=active_filters)
        except UnauthorizedError:
            if self._is_widget_alive():
                self._signals.unauthorized.emit(request_id, "items")
            return
        except ApiError as exc:
            if self._is_widget_alive():
                self._signals.failed.emit(str(exc), request_id, "items")
            return
        if self._is_widget_alive():
            self._signals.items_loaded.emit(request_id, items, total)
```

```python
def _handle_category_row_changed(self, row: int) -> None:
    if not (0 <= row < len(self.categories)):
        return
    previous_category = self.selected_category_id
    if previous_category:
        self._remember_current_filter_state()
    category = self.categories[row]
    self.selected_category_id = category.type_id
    self._category_filter_state.setdefault(category.type_id, self._default_filter_state(category))
    self._rebuild_filter_panel()
    self.current_page = 1
    self.reset_folder_breadcrumbs_to_root()
    if self._search_mode:
        return
    self.load_items(self.selected_category_id, self.current_page)
```

```python
def search(self) -> None:
    if not self._search_enabled:
        return
    keyword = self.keyword_edit.text().strip()
    if not keyword:
        self.clear_search()
        return
    self._search_mode = True
    self.filter_toggle_button.hide()
    self.filter_panel.hide()
    self._search_keyword = keyword
    self.current_page = 1
    self._search_items(keyword, self.current_page)


def clear_search(self) -> None:
    if not self._search_enabled:
        return
    self.keyword_edit.clear()
    self._search_mode = False
    self._search_keyword = ""
    self.current_page = 1
    self._rebuild_filter_panel()
    if self.selected_category_id:
        self.load_items(self.selected_category_id, self.current_page)
```

- [ ] **Step 4: Re-run the focused poster-grid tests to verify they pass**

Run:

```bash
uv run pytest tests/test_poster_grid_page_ui.py::test_poster_grid_page_hides_filter_button_by_default tests/test_poster_grid_page_ui.py::test_poster_grid_page_shows_filter_button_for_filtered_category_and_stays_collapsed tests/test_poster_grid_page_ui.py::test_poster_grid_page_expands_filters_and_reloads_page_one_on_change tests/test_poster_grid_page_ui.py::test_poster_grid_page_remembers_filter_state_per_category tests/test_poster_grid_page_ui.py::test_poster_grid_page_hides_category_filters_during_search_and_restores_them_after_clear -v
```

Expected: PASS for all five tests.

- [ ] **Step 5: Commit the poster-grid filter UI slice**

Run:

```bash
git add tests/test_poster_grid_page_ui.py src/atv_player/ui/poster_grid_page.py
git commit -m "feat: add poster grid category filters"
```

### Task 3: Align Non-Plugin Poster Controllers With The Shared Filter Signature

**Files:**
- Modify: `tests/test_douban_controller.py`
- Modify: `tests/test_live_controller.py`
- Modify: `tests/test_emby_controller.py`
- Modify: `tests/test_jellyfin_controller.py`
- Modify: `tests/test_telegram_search_controller.py`
- Modify: `src/atv_player/controllers/douban_controller.py`
- Modify: `src/atv_player/controllers/live_controller.py`
- Modify: `src/atv_player/controllers/emby_controller.py`
- Modify: `src/atv_player/controllers/jellyfin_controller.py`
- Modify: `src/atv_player/controllers/telegram_search_controller.py`

- [ ] **Step 1: Write the failing shared-controller signature tests**

Add one no-op filter argument test per poster-grid controller:

```python
def test_douban_controller_ignores_optional_filters_argument() -> None:
    api = FakeApiClient()
    controller = DoubanController(api)

    controller.load_items("movie", page=1, filters={"sc": "6"})

    assert api.calls[-1] == ("list_douban_items", "movie", 1, 30)
```

```python
def test_live_controller_ignores_optional_filters_argument() -> None:
    api = FakeApiClient()
    controller = LiveController(api)

    controller.load_items("bili", 1, filters={"status": "1"})

    assert api.calls[-1] == ("list_live_items", "bili", 1, 30)
```

```python
def test_emby_controller_ignores_optional_filters_argument() -> None:
    api = FakeApiClient()
    controller = EmbyController(api)

    controller.load_items("Movie", 1, filters={"status": "1"})

    assert api.calls[-1] == ("list_emby_items", "Movie", 1, 30)
```

```python
def test_jellyfin_controller_ignores_optional_filters_argument() -> None:
    api = FakeApiClient()
    controller = JellyfinController(api)

    controller.load_items("Movie", 1, filters={"status": "1"})

    assert api.calls[-1] == ("list_jellyfin_items", "Movie", 1, 30)
```

```python
def test_telegram_search_controller_ignores_optional_filters_argument() -> None:
    api = FakeApiClient()
    controller = TelegramSearchController(api)

    controller.load_items("Movie", page=1, filters={"status": "1"})

    assert api.calls[-1] == ("list_telegram_search_items", "Movie", 1)
```

- [ ] **Step 2: Run the shared-controller tests to verify they fail**

Run:

```bash
uv run pytest tests/test_douban_controller.py::test_douban_controller_ignores_optional_filters_argument tests/test_live_controller.py::test_live_controller_ignores_optional_filters_argument tests/test_emby_controller.py::test_emby_controller_ignores_optional_filters_argument tests/test_jellyfin_controller.py::test_jellyfin_controller_ignores_optional_filters_argument tests/test_telegram_search_controller.py::test_telegram_search_controller_ignores_optional_filters_argument -v
```

Expected: FAIL with `TypeError` because those `load_items()` methods do not yet accept `filters`.

- [ ] **Step 3: Extend the shared controller signatures as no-ops**

Update each controller to accept and ignore `filters`:

```python
def load_items(
    self,
    category_id: str,
    page: int,
    filters: dict[str, str] | None = None,
) -> tuple[list[VodItem], int]:
    payload = self._api_client.list_douban_items(category_id, page=page, size=self._PAGE_SIZE)
    items = [_map_item(item) for item in payload.get("list", [])]
    total_raw = payload.get("total")
    if total_raw is not None:
        total = int(total_raw)
    else:
        pagecount = int(payload.get("pagecount") or 0)
        total = pagecount * self._PAGE_SIZE
    return items, total
```

```python
def load_items(
    self,
    category_id: str,
    page: int,
    filters: dict[str, str] | None = None,
) -> tuple[list[VodItem], int]:
    if category_id.startswith("custom:"):
        return self._custom_live_service.load_items(category_id, page)
    payload = self._api_client.list_live_items(category_id, page=page, size=self._PAGE_SIZE)
    items = [_map_item(item) for item in payload.get("list", [])]
    total_raw = payload.get("total")
    if total_raw is not None:
        total = int(total_raw)
    else:
        pagecount = int(payload.get("pagecount") or 0)
        total = pagecount * self._PAGE_SIZE
    return items, total
```

```python
def load_items(
    self,
    category_id: str,
    page: int,
    filters: dict[str, str] | None = None,
) -> tuple[list[VodItem], int]:
    payload = self._api_client.list_emby_items(category_id, page=page, size=self._PAGE_SIZE)
    items = [_map_item(item) for item in payload.get("list", [])]
    total = int(payload.get("total") or len(items))
    return items, total
```

```python
def load_items(
    self,
    category_id: str,
    page: int,
    filters: dict[str, str] | None = None,
) -> tuple[list[VodItem], int]:
    payload = self._api_client.list_jellyfin_items(category_id, page=page, size=self._PAGE_SIZE)
    items = [_map_item(item) for item in payload.get("list", [])]
    total = int(payload.get("total") or len(items))
    return items, total
```

```python
def load_items(
    self,
    category_id: str,
    page: int,
    filters: dict[str, str] | None = None,
) -> tuple[list[VodItem], int]:
    payload = self._api_client.list_telegram_search_items(category_id, page=page)
    items = [_map_item(item) for item in payload.get("list", [])]
    total = int(payload.get("total") or len(items))
    return items, total
```

- [ ] **Step 4: Run the shared-controller tests and the end-to-end poster-grid regression tests**

Run:

```bash
uv run pytest tests/test_douban_controller.py::test_douban_controller_ignores_optional_filters_argument tests/test_live_controller.py::test_live_controller_ignores_optional_filters_argument tests/test_emby_controller.py::test_emby_controller_ignores_optional_filters_argument tests/test_jellyfin_controller.py::test_jellyfin_controller_ignores_optional_filters_argument tests/test_telegram_search_controller.py::test_telegram_search_controller_ignores_optional_filters_argument tests/test_spider_plugin_controller.py tests/test_poster_grid_page_ui.py -q
```

Expected: PASS with the new filter-aware behavior covered and all existing poster-grid regressions still green.

- [ ] **Step 5: Commit the shared controller signature alignment**

Run:

```bash
git add tests/test_douban_controller.py tests/test_live_controller.py tests/test_emby_controller.py tests/test_jellyfin_controller.py tests/test_telegram_search_controller.py src/atv_player/controllers/douban_controller.py src/atv_player/controllers/live_controller.py src/atv_player/controllers/emby_controller.py src/atv_player/controllers/jellyfin_controller.py src/atv_player/controllers/telegram_search_controller.py
git commit -m "refactor: align poster grid controller filter signatures"
```

### Task 4: Final Verification

**Files:**
- Modify: none
- Test: `tests/test_spider_plugin_controller.py`
- Test: `tests/test_poster_grid_page_ui.py`
- Test: `tests/test_douban_controller.py`
- Test: `tests/test_live_controller.py`
- Test: `tests/test_emby_controller.py`
- Test: `tests/test_jellyfin_controller.py`
- Test: `tests/test_telegram_search_controller.py`

- [ ] **Step 1: Run the full targeted verification suite**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py tests/test_poster_grid_page_ui.py tests/test_douban_controller.py tests/test_live_controller.py tests/test_emby_controller.py tests/test_jellyfin_controller.py tests/test_telegram_search_controller.py -q
```

Expected: PASS

- [ ] **Step 2: Record the completed state**

Run:

```bash
git status --short
```

Expected: either a clean worktree or only intentional uncommitted changes outside this plan.
