# Poster Grid Filter Button Groups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace poster-grid category filter combo boxes with single-select button groups that wrap across lines while preserving current per-category filter behavior.

**Architecture:** Keep filter state as `dict[str, str]` keyed by category id and continue passing only non-empty selections into controller `load_items(...)`. Replace the filter panel's per-group input from `QComboBox` to a lightweight flow-layout container of checkable buttons, and update the UI tests to assert selected buttons instead of combo-box state.

**Tech Stack:** Python 3.14, PySide6 widgets/layouts, pytest, pytest-qt

---

### Task 1: Lock In Button-Group UI Behavior With Tests

**Files:**
- Modify: `tests/test_poster_grid_page_ui.py`
- Test: `tests/test_poster_grid_page_ui.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_poster_grid_page_renders_filter_options_as_checkable_buttons(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FilterablePosterController(), click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    buttons = page.filter_buttons["sc"]

    assert [button.text() for button in buttons] == ["默认", "不限", "动作"]
    assert buttons[0].isCheckable() is True
    assert buttons[0].isChecked() is True
```

```python
def test_poster_grid_page_clicking_filter_button_selects_it_and_reloads(qtbot) -> None:
    controller = FilterablePosterController()
    page = show_loaded_page(qtbot, PosterGridPage(controller, click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    default_button, _, action_button = page.filter_buttons["sc"]
    action_button.click()

    qtbot.waitUntil(lambda: controller.filtered_item_calls[-1] == ("movie", 1, {"sc": "6"}))
    assert default_button.isChecked() is False
    assert action_button.isChecked() is True
```

```python
def test_poster_grid_page_uses_plugin_empty_filter_button_without_extra_default(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(EmptyValueFilterPosterController(), click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    buttons = page.filter_buttons["class"]

    assert [button.text() for button in buttons] == ["全部", "爱情"]
    assert buttons[0].isChecked() is True
```
```

- [ ] **Step 2: Run the focused UI tests to verify they fail**

Run: `uv run pytest tests/test_poster_grid_page_ui.py::test_poster_grid_page_renders_filter_options_as_checkable_buttons tests/test_poster_grid_page_ui.py::test_poster_grid_page_clicking_filter_button_selects_it_and_reloads tests/test_poster_grid_page_ui.py::test_poster_grid_page_uses_plugin_empty_filter_button_without_extra_default -v`

Expected: FAIL because `PosterGridPage` still exposes `filter_combos` and does not render per-group checkable button lists.

- [ ] **Step 3: Update the existing filter UI tests to use button-group assertions**

```python
def _checked_filter_value(page: PosterGridPage, key: str) -> str:
    for button in page.filter_buttons[key]:
        if button.isChecked():
            return str(button.property("filterValue") or "")
    return ""


def test_poster_grid_page_remembers_filter_state_per_category(qtbot) -> None:
    controller = FilterablePosterController()
    page = show_loaded_page(qtbot, PosterGridPage(controller, click_action="open", search_enabled=False))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.filter_toggle_button.click()
    next(button for button in page.filter_buttons["sc"] if button.property("filterValue") == "6").click()
    qtbot.waitUntil(lambda: controller.filtered_item_calls[-1] == ("movie", 1, {"sc": "6"}))

    page.category_list.setCurrentRow(1)
    qtbot.waitUntil(lambda: controller.filtered_item_calls[-1] == ("tv", 1, {}))

    page.category_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: _checked_filter_value(page, "sc") == "6")
```

- [ ] **Step 4: Run the focused UI tests again to verify the failures are specific**

Run: `uv run pytest tests/test_poster_grid_page_ui.py -k "filter_button or filter_controls or remembers_filter_state" -v`

Expected: FAIL on missing button-group implementation, not on syntax or unrelated UI setup errors.

### Task 2: Replace Combo Boxes With Wrapping Button Groups

**Files:**
- Modify: `src/atv_player/ui/poster_grid_page.py`
- Test: `tests/test_poster_grid_page_ui.py`

- [ ] **Step 1: Add a lightweight wrapping layout and button-group storage**

```python
class _FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None
```

```python
self.filter_buttons: dict[str, list[QPushButton]] = {}
```

- [ ] **Step 2: Run the new focused tests to confirm they still fail**

Run: `uv run pytest tests/test_poster_grid_page_ui.py::test_poster_grid_page_renders_filter_options_as_checkable_buttons -v`

Expected: FAIL because buttons are not yet rendered or wired to selection logic.

- [ ] **Step 3: Render each filter group as a row of checkable buttons and preserve empty-option behavior**

```python
def _build_filter_buttons(self, group, selected_value: str) -> QWidget:
    container = QWidget()
    layout = _FlowLayout(container)
    options = list(group.options)
    if not any(option.value == "" for option in options):
        options = [CategoryFilterOption(name="默认", value=""), *options]

    buttons: list[QPushButton] = []
    for option in options:
        button = QPushButton(option.name)
        button.setCheckable(True)
        button.setProperty("filterKey", group.key)
        button.setProperty("filterValue", option.value)
        button.setChecked(option.value == selected_value)
        button.clicked.connect(self._handle_filter_button_clicked)
        layout.addWidget(button)
        buttons.append(button)
    self.filter_buttons[group.key] = buttons
    return container
```

- [ ] **Step 4: Move selection extraction from combo-box state to checked-button state**

```python
def _selected_filter_values(self) -> dict[str, str]:
    selected: dict[str, str] = {}
    for key, buttons in self.filter_buttons.items():
        checked = next((button for button in buttons if button.isChecked()), None)
        if checked is None:
            continue
        value = str(checked.property("filterValue") or "")
        if value:
            selected[key] = value
    return selected
```

```python
def _handle_filter_button_clicked(self) -> None:
    button = cast(QPushButton, self.sender())
    key = str(button.property("filterKey") or "")
    if not key:
        return
    for candidate in self.filter_buttons.get(key, []):
        candidate.setChecked(candidate is button)
    self._handle_filter_changed()
```

- [ ] **Step 5: Replace combo-box setup/teardown with button-group setup/teardown**

```python
while self.filter_panel_layout.rowCount():
    self.filter_panel_layout.removeRow(0)
self.filter_buttons = {}

for group in filters:
    if not group.options:
        continue
    selected_value = state.get(group.key, "")
    buttons_widget = self._build_filter_buttons(group, selected_value)
    self.filter_panel_layout.addRow(group.name, buttons_widget)
```

- [ ] **Step 6: Run the focused tests to verify the button-group implementation passes**

Run: `uv run pytest tests/test_poster_grid_page_ui.py -k "filter_button or filter_controls or remembers_filter_state" -v`

Expected: PASS

### Task 3: Regression Verification

**Files:**
- Modify: `src/atv_player/ui/poster_grid_page.py`
- Test: `tests/test_poster_grid_page_ui.py`, `tests/test_spider_plugin_controller.py`, `tests/test_douban_controller.py`, `tests/test_emby_controller.py`, `tests/test_jellyfin_controller.py`, `tests/test_api_client.py`, `tests/test_live_controller.py`, `tests/test_telegram_search_controller.py`

- [ ] **Step 1: Run the broader regression suite**

Run: `uv run pytest tests/test_api_client.py tests/test_douban_controller.py tests/test_emby_controller.py tests/test_jellyfin_controller.py tests/test_live_controller.py tests/test_telegram_search_controller.py tests/test_poster_grid_page_ui.py tests/test_spider_plugin_controller.py -q`

Expected: PASS

- [ ] **Step 2: Review the final diff for scope**

Run: `git diff -- src/atv_player/ui/poster_grid_page.py tests/test_poster_grid_page_ui.py`

Expected: only the filter UI implementation and its tests changed for this phase.

- [ ] **Step 3: Commit the button-group change**

```bash
git add src/atv_player/ui/poster_grid_page.py tests/test_poster_grid_page_ui.py
git commit -m "feat: use button groups for poster filters"
```
