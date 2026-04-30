# Poster Grid Filter Button Styling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add light-theme-inspired styling to poster-grid filter buttons and make every clickable button in `PosterGridPage` use the pointing-hand cursor.

**Architecture:** Keep the styling local to `PosterGridPage` instead of introducing a shared theme abstraction. Centralize button cursor setup in small helper methods so filter buttons, action buttons, pagination buttons, breadcrumb buttons, and poster cards all get a consistent pointing-hand cursor without duplicating widget setup logic.

**Tech Stack:** Python 3.14, PySide6 widgets and stylesheets, pytest, pytest-qt

---

### Task 1: Lock In Styling and Cursor Behavior With Tests

**Files:**
- Modify: `tests/test_poster_grid_page_ui.py`
- Test: `tests/test_poster_grid_page_ui.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_poster_grid_page_filter_buttons_use_light_theme_stylesheet(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FilterablePosterController(), click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    button = page.filter_buttons["sc"][0]
    stylesheet = button.styleSheet()

    assert "background-color: #ffffff;" in stylesheet
    assert "border: 1px solid #d0d0d0;" in stylesheet
    assert "color: #1a1a1a;" in stylesheet
    assert "QPushButton:hover" in stylesheet
    assert "#e8e8e8" in stylesheet
    assert "QPushButton:checked" in stylesheet
    assert "#0066cc" in stylesheet
    assert "#0080ff" in stylesheet
```

```python
def test_poster_grid_page_sets_pointing_hand_cursor_for_all_clickable_buttons(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FilterablePosterController(), click_action="open", search_enabled=True, folder_navigation_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    clickable_buttons = [
        page.search_button,
        page.clear_button,
        page.filter_toggle_button,
        page.prev_page_button,
        page.next_page_button,
        page.card_buttons[0],
        page.filter_buttons["sc"][0],
    ]

    assert all(button.cursor().shape() == Qt.CursorShape.PointingHandCursor for button in clickable_buttons)
```

```python
def test_poster_grid_page_breadcrumb_buttons_use_pointing_hand_cursor(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController(), folder_navigation_enabled=True))

    qtbot.waitUntil(lambda: len(page.breadcrumb_buttons) == 2)

    assert all(button.cursor().shape() == Qt.CursorShape.PointingHandCursor for button in page.breadcrumb_buttons)
```
```

- [ ] **Step 2: Run the focused styling tests to verify they fail**

Run: `uv run pytest tests/test_poster_grid_page_ui.py::test_poster_grid_page_filter_buttons_use_light_theme_stylesheet tests/test_poster_grid_page_ui.py::test_poster_grid_page_sets_pointing_hand_cursor_for_all_clickable_buttons tests/test_poster_grid_page_ui.py::test_poster_grid_page_breadcrumb_buttons_use_pointing_hand_cursor -v`

Expected: FAIL because filter buttons do not yet have a local stylesheet and several `PosterGridPage` buttons still use the default arrow cursor.

- [ ] **Step 3: Update any existing cursor assertions to reflect the broader page-wide cursor rule**

```python
def test_poster_grid_page_card_buttons_use_hand_cursor(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController()))

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)

    assert page.card_buttons[0].cursor().shape() == Qt.CursorShape.PointingHandCursor
```

- [ ] **Step 4: Run the focused styling tests again to verify failures are specific**

Run: `uv run pytest tests/test_poster_grid_page_ui.py -k "light_theme_stylesheet or pointing_hand_cursor or breadcrumb_buttons_use_pointing_hand_cursor" -v`

Expected: FAIL only on the missing implementation, not on unrelated syntax or setup errors.

### Task 2: Implement Local Filter Styles and Centralized Cursor Setup

**Files:**
- Modify: `src/atv_player/ui/poster_grid_page.py`
- Test: `tests/test_poster_grid_page_ui.py`

- [ ] **Step 1: Add helper methods for button cursor and filter stylesheet application**

```python
def _set_button_cursor(self, button: QPushButton | QToolButton) -> None:
    button.setCursor(Qt.CursorShape.PointingHandCursor)


def _apply_filter_button_style(self, button: QPushButton) -> None:
    button.setStyleSheet(
        """
        QPushButton {
            background-color: #ffffff;
            color: #1a1a1a;
            border: 1px solid #d0d0d0;
            border-radius: 14px;
            padding: 6px 12px;
        }
        QPushButton:hover {
            background-color: #e8e8e8;
        }
        QPushButton:checked {
            color: #0066cc;
            border: 1px solid #0066cc;
        }
        QPushButton:checked:hover {
            color: #0080ff;
            border: 1px solid #0080ff;
        }
        """
    )
```

- [ ] **Step 2: Run one focused test to confirm it still fails before wiring usage**

Run: `uv run pytest tests/test_poster_grid_page_ui.py::test_poster_grid_page_filter_buttons_use_light_theme_stylesheet -v`

Expected: FAIL because the helper exists but is not yet applied to created buttons.

- [ ] **Step 3: Apply the pointing-hand cursor to all clickable buttons during construction**

```python
for button in (
    self.search_button,
    self.clear_button,
    self.filter_toggle_button,
    self.prev_page_button,
    self.next_page_button,
):
    self._set_button_cursor(button)
```

```python
button = QPushButton(option.name, container)
self._set_button_cursor(button)
self._apply_filter_button_style(button)
```

```python
button = QPushButton(breadcrumb["label"])
self._set_button_cursor(button)
```

```python
button = QToolButton()
self._set_button_cursor(button)
```

- [ ] **Step 4: Keep the styling scoped to filter option buttons**

```python
def _build_filter_buttons(self, key: str, options, selected_value: str) -> QWidget:
    ...
    button = QPushButton(option.name, container)
    self._set_button_cursor(button)
    self._apply_filter_button_style(button)
    ...
```

Expected result: action buttons only receive the cursor change, while filter option buttons receive both cursor and local stylesheet.

- [ ] **Step 5: Run the focused styling tests to verify the implementation passes**

Run: `uv run pytest tests/test_poster_grid_page_ui.py -k "light_theme_stylesheet or pointing_hand_cursor or breadcrumb_buttons_use_pointing_hand_cursor" -v`

Expected: PASS

### Task 3: Regression Verification

**Files:**
- Modify: `src/atv_player/ui/poster_grid_page.py`
- Test: `tests/test_poster_grid_page_ui.py`, `tests/test_api_client.py`, `tests/test_douban_controller.py`, `tests/test_emby_controller.py`, `tests/test_jellyfin_controller.py`, `tests/test_live_controller.py`, `tests/test_telegram_search_controller.py`, `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Run the broader regression suite**

Run: `uv run pytest tests/test_api_client.py tests/test_douban_controller.py tests/test_emby_controller.py tests/test_jellyfin_controller.py tests/test_live_controller.py tests/test_telegram_search_controller.py tests/test_poster_grid_page_ui.py tests/test_spider_plugin_controller.py -q`

Expected: PASS

- [ ] **Step 2: Review the final diff for scope**

Run: `git diff -- src/atv_player/ui/poster_grid_page.py tests/test_poster_grid_page_ui.py`

Expected: only filter button styling, cursor helpers, and related UI tests changed.

- [ ] **Step 3: Commit the styling update**

```bash
git add src/atv_player/ui/poster_grid_page.py tests/test_poster_grid_page_ui.py
git commit -m "style: polish poster filter buttons"
```
