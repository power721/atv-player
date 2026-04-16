# Media Folder Breadcrumb Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add clickable folder breadcrumbs to the Live, Emby, and Jellyfin tabs so users can navigate back to parent levels after entering folders.

**Architecture:** Extend `DoubanPage` with an optional breadcrumb/navigation-stack UI that emits navigation intents without directly loading data. Keep loading logic in `MainWindow`, which already owns the folder-click handling for live, Emby, and Jellyfin, and make it update or rewind the page breadcrumb state around controller calls.

**Tech Stack:** Python, PySide6, pytest-qt

---

### Task 1: Lock Down Breadcrumb UI Behavior In `DoubanPage`

**Files:**
- Modify: `src/atv_player/ui/douban_page.py`
- Test: `tests/test_douban_page_ui.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_douban_page_can_render_folder_breadcrumbs_when_navigation_enabled(qtbot) -> None:
    page = show_loaded_page(qtbot, DoubanPage(FakeDoubanController(), folder_navigation_enabled=True))
    qtbot.waitUntil(lambda: page.category_list.count() == 2)

    page.set_folder_breadcrumbs(
        [
            {"id": "home", "label": "首页", "kind": "home"},
            {"id": "suggestion", "label": "推荐", "kind": "category"},
            {"id": "folder-1", "label": "分区", "kind": "folder"},
        ]
    )

    assert [button.text() for button in page.breadcrumb_buttons] == ["首页", "推荐", "分区"]


def test_douban_page_clicking_breadcrumb_emits_navigation_request(qtbot) -> None:
    page = show_loaded_page(qtbot, DoubanPage(FakeDoubanController(), folder_navigation_enabled=True))
    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    page.set_folder_breadcrumbs(
        [
            {"id": "home", "label": "首页", "kind": "home"},
            {"id": "suggestion", "label": "推荐", "kind": "category"},
            {"id": "folder-1", "label": "分区", "kind": "folder"},
        ]
    )

    with qtbot.waitSignal(page.folder_breadcrumb_requested, timeout=1000) as signal:
        page.breadcrumb_buttons[1].click()

    assert signal.args == ["suggestion", "category", 1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_douban_page_ui.py -k breadcrumb -v`
Expected: FAIL because `DoubanPage` has no breadcrumb API or signal for folder navigation.

- [ ] **Step 3: Write minimal implementation**

```python
class DoubanPage(QWidget):
    folder_breadcrumb_requested = Signal(str, str, int)

    def __init__(..., folder_navigation_enabled: bool = False) -> None:
        self._folder_navigation_enabled = folder_navigation_enabled
        self.folder_breadcrumbs: list[dict[str, object]] = []
        self.breadcrumb_bar = QWidget()
        self.breadcrumb_layout = QHBoxLayout(self.breadcrumb_bar)
        self.breadcrumb_buttons: list[QPushButton] = []

    def set_folder_breadcrumbs(self, breadcrumbs) -> None:
        self.folder_breadcrumbs = list(breadcrumbs)
        self._render_folder_breadcrumbs()

    def _render_folder_breadcrumbs(self) -> None:
        ...

    def _handle_folder_breadcrumb_clicked(self, index: int) -> None:
        node = self.folder_breadcrumbs[index]
        self.folder_breadcrumb_requested.emit(node["id"], node["kind"], index)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_douban_page_ui.py -k breadcrumb -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_douban_page_ui.py src/atv_player/ui/douban_page.py
git commit -m "feat: add media page breadcrumb ui"
```

### Task 2: Drive Live/Emby/Jellyfin Navigation Through Breadcrumb State

**Files:**
- Modify: `src/atv_player/ui/main_window.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_main_window_live_breadcrumb_click_loads_category_root(qtbot, monkeypatch) -> None:
    controller = FakeLiveController()
    window = MainWindow(..., live_controller=controller, ...)
    qtbot.addWidget(window)
    window.show()

    shown = []
    monkeypatch.setattr(window.live_page, "show_items", lambda items, total, page=1, empty_message="当前分类暂无内容": shown.append((items, total, page, empty_message)))

    window.live_page.selected_category_id = "bili"
    window._push_media_folder(window.live_page, "分区", "bili-9", controller.load_folder_items)
    window.live_page.folder_breadcrumb_requested.emit("bili", "category", 1)

    assert controller.item_calls[-1] == ("bili", 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_app.py -k breadcrumb -v`
Expected: FAIL because `MainWindow` does not listen for breadcrumb navigation events.

- [ ] **Step 3: Write minimal implementation**

```python
class MainWindow(QMainWindow):
    def _reset_media_breadcrumbs(self, page: DoubanPage) -> None:
        ...

    def _push_media_folder(self, page: DoubanPage, label: str, vod_id: str, loader) -> None:
        ...

    def _handle_media_breadcrumb_requested(self, page: DoubanPage, kind: str, node_id: str) -> None:
        ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_app.py -k breadcrumb -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_app.py src/atv_player/ui/main_window.py
git commit -m "feat: wire media breadcrumb navigation"
```

### Task 3: Run Focused Regression Verification

**Files:**
- Modify: `tests/test_douban_page_ui.py`
- Modify: `tests/test_app.py`
- Modify: `src/atv_player/ui/douban_page.py`
- Modify: `src/atv_player/ui/main_window.py`

- [ ] **Step 1: Run the focused UI tests**

```bash
uv run pytest tests/test_douban_page_ui.py tests/test_app.py -k "breadcrumb or live or emby or jellyfin" -v
```

Expected: PASS for the new breadcrumb coverage and existing media-tab behavior.

- [ ] **Step 2: Run the broader affected suites**

```bash
uv run pytest tests/test_douban_page_ui.py tests/test_app.py -v
```

Expected: PASS with no regressions in the existing media-tab and card-click flows.

- [ ] **Step 3: Commit**

```bash
git add src/atv_player/ui/douban_page.py src/atv_player/ui/main_window.py tests/test_douban_page_ui.py tests/test_app.py
git commit -m "test: verify media breadcrumb navigation"
```
