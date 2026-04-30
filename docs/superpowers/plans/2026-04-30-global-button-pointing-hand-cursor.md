# Global Button Pointing-Hand Cursor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all `QPushButton` and `QToolButton` instances in the app use `Qt.CursorShape.PointingHandCursor` by default.

**Architecture:** Install a lightweight application-level event filter from `build_application()` so every button, including dynamically created ones, receives the pointing-hand cursor without page-by-page duplication. Keep page-specific filter button styling in `PosterGridPage`, but remove any cursor logic there that becomes redundant once the app-wide installer is active.

**Tech Stack:** Python 3.14, PySide6 event filters, pytest, pytest-qt

---

### Task 1: Lock In Application-Level Cursor Behavior With Tests

**Files:**
- Modify: `tests/test_app.py`
- Modify: `tests/test_poster_grid_page_ui.py`
- Test: `tests/test_app.py`
- Test: `tests/test_poster_grid_page_ui.py`

- [ ] **Step 1: Write the failing application-level test**

```python
def test_build_application_installs_pointing_hand_cursor_for_buttons(monkeypatch, tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(app_module, "QApplication", lambda args: app)
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    built_app, _repo = app_module.build_application()
    assert built_app is app

    push_button = QPushButton("Push")
    tool_button = QToolButton()
    push_button.show()
    tool_button.show()
    app.processEvents()

    assert push_button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert tool_button.cursor().shape() == Qt.CursorShape.PointingHandCursor
```

```python
def test_build_application_does_not_change_non_button_cursor(monkeypatch, tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(app_module, "QApplication", lambda args: app)
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    app_module.build_application()

    line_edit = QLineEdit()
    line_edit.show()
    app.processEvents()

    assert line_edit.cursor().shape() != Qt.CursorShape.PointingHandCursor
```
```

- [ ] **Step 2: Run the focused app tests to verify they fail**

Run: `uv run pytest tests/test_app.py::test_build_application_installs_pointing_hand_cursor_for_buttons tests/test_app.py::test_build_application_does_not_change_non_button_cursor -v`

Expected: FAIL because `build_application()` does not yet install any app-wide cursor behavior.

- [ ] **Step 3: Add a cross-page regression assertion that now depends on app-level setup**

```python
def test_poster_grid_page_buttons_use_pointing_hand_cursor(qtbot) -> None:
    page = show_loaded_page(
        qtbot,
        PosterGridPage(FilterablePosterController(), click_action="open", search_enabled=True, folder_navigation_enabled=True),
    )

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    assert all(
        button.cursor().shape() == Qt.CursorShape.PointingHandCursor
        for button in (
            page.search_button,
            page.clear_button,
            page.filter_toggle_button,
            page.prev_page_button,
            page.next_page_button,
            page.card_buttons[0],
            page.filter_buttons["sc"][0],
        )
    )
```

- [ ] **Step 4: Run the combined focused tests to verify failures are specific**

Run: `uv run pytest tests/test_app.py::test_build_application_installs_pointing_hand_cursor_for_buttons tests/test_app.py::test_build_application_does_not_change_non_button_cursor tests/test_poster_grid_page_ui.py::test_poster_grid_page_sets_pointing_hand_cursor_for_all_clickable_buttons -v`

Expected: FAIL only on missing app-wide cursor installation.

### Task 2: Install the Global Button Cursor Hook

**Files:**
- Modify: `src/atv_player/app.py`
- Modify: `src/atv_player/ui/poster_grid_page.py`
- Test: `tests/test_app.py`
- Test: `tests/test_poster_grid_page_ui.py`

- [ ] **Step 1: Add a small application event filter for buttons**

```python
class _ButtonCursorEventFilter(QObject):
    def eventFilter(self, watched, event) -> bool:
        if isinstance(watched, QPushButton | QToolButton):
            watched.setCursor(Qt.CursorShape.PointingHandCursor)
        return False
```

```python
def _install_button_pointing_hand_cursor(app: QApplication) -> None:
    filter_obj = _ButtonCursorEventFilter(app)
    app.installEventFilter(filter_obj)
    setattr(app, "_button_cursor_event_filter", filter_obj)
```

- [ ] **Step 2: Run one focused app test to confirm it still fails before wiring installation**

Run: `uv run pytest tests/test_app.py::test_build_application_installs_pointing_hand_cursor_for_buttons -v`

Expected: FAIL because the event filter exists but `build_application()` still does not install it.

- [ ] **Step 3: Install the event filter during application construction**

```python
def build_application() -> tuple[QApplication, SettingsRepository]:
    app = QApplication([])
    _install_button_pointing_hand_cursor(app)
    app.setApplicationName("atv-player")
    ...
```

- [ ] **Step 4: Remove redundant page-local cursor setup that the global hook now guarantees**

```python
def _build_filter_buttons(self, key: str, options, selected_value: str) -> QWidget:
    ...
    button = QPushButton(option.name, container)
    self._apply_filter_button_style(button)
```

```python
def _render_folder_breadcrumbs(self) -> None:
    ...
    button = QPushButton(breadcrumb["label"])
    button.setFlat(True)
```

```python
def _build_card_button(self, item) -> QToolButton:
    button = QToolButton()
    ...
```

Expected result: cursor behavior is centralized in the app layer, while `PosterGridPage` retains only style and behavior that are specific to that page.

- [ ] **Step 5: Run the focused cursor tests to verify the implementation passes**

Run: `uv run pytest tests/test_app.py::test_build_application_installs_pointing_hand_cursor_for_buttons tests/test_app.py::test_build_application_does_not_change_non_button_cursor tests/test_poster_grid_page_ui.py::test_poster_grid_page_sets_pointing_hand_cursor_for_all_clickable_buttons tests/test_poster_grid_page_ui.py::test_poster_grid_page_breadcrumb_buttons_use_pointing_hand_cursor -v`

Expected: PASS

### Task 3: Regression Verification

**Files:**
- Modify: `src/atv_player/app.py`
- Modify: `src/atv_player/ui/poster_grid_page.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_poster_grid_page_ui.py`
- Test: `tests/test_api_client.py`, `tests/test_app.py`, `tests/test_douban_controller.py`, `tests/test_emby_controller.py`, `tests/test_jellyfin_controller.py`, `tests/test_live_controller.py`, `tests/test_telegram_search_controller.py`, `tests/test_poster_grid_page_ui.py`, `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Run the broader regression suite**

Run: `uv run pytest tests/test_api_client.py tests/test_app.py tests/test_douban_controller.py tests/test_emby_controller.py tests/test_jellyfin_controller.py tests/test_live_controller.py tests/test_telegram_search_controller.py tests/test_poster_grid_page_ui.py tests/test_spider_plugin_controller.py -q`

Expected: PASS

- [ ] **Step 2: Review the final diff for scope**

Run: `git diff -- src/atv_player/app.py src/atv_player/ui/poster_grid_page.py tests/test_app.py tests/test_poster_grid_page_ui.py`

Expected: only the global button cursor installer, redundant cursor cleanup, and related tests changed.

- [ ] **Step 3: Commit the global cursor update**

```bash
git add src/atv_player/app.py src/atv_player/ui/poster_grid_page.py tests/test_app.py tests/test_poster_grid_page_ui.py
git commit -m "feat: use pointing hand cursor for app buttons"
```
