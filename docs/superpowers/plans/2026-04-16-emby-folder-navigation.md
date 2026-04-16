# Emby Folder Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Emby cards open folders in-place inside the Emby tab while still playing file items directly.

**Architecture:** Extend the shared poster-grid page with an item-level click signal, route only Emby through that richer signal in `MainWindow`, and add an Emby controller/API path that loads folder children by `ids`. This keeps Telegram unchanged and avoids building a separate Emby page.

**Tech Stack:** Python, PySide6, pytest, httpx

---

### Task 1: Document the richer Emby click contract in tests

**Files:**
- Modify: `tests/test_app.py`
- Modify: `tests/test_emby_controller.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_main_window_emby_folder_click_loads_folder_in_current_tab(...):
    ...

def test_load_folder_items_maps_emby_ids_payload() -> None:
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/test_app.py tests/test_emby_controller.py`
Expected: FAIL because Emby has no folder-loading path and MainWindow treats all clicks as playback requests.

- [ ] **Step 3: Write minimal implementation**

```python
class EmbyController:
    def load_folder_items(self, vod_id: str) -> list[VodItem]:
        payload = self._api_client.get_emby_detail(vod_id)
        return [_map_item(item) for item in payload.get("list", [])]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/test_app.py tests/test_emby_controller.py`
Expected: PASS for the new controller and main-window behaviors.

- [ ] **Step 5: Commit**

```bash
git add tests/test_app.py tests/test_emby_controller.py src/atv_player/controllers/emby_controller.py src/atv_player/ui/main_window.py src/atv_player/ui/douban_page.py
git commit -m "feat: add emby folder navigation"
```

### Task 2: Preserve shared page behavior while exposing full clicked items

**Files:**
- Modify: `src/atv_player/ui/douban_page.py`
- Test: `tests/test_douban_page_ui.py`

- [ ] **Step 1: Write the failing test**

```python
def test_douban_page_clicking_card_emits_item_open_requested(qtbot) -> None:
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_douban_page_ui.py::test_douban_page_clicking_card_emits_item_open_requested`
Expected: FAIL because the page only emits `open_requested(str)`.

- [ ] **Step 3: Write minimal implementation**

```python
class DoubanPage(QWidget):
    item_open_requested = Signal(object)

    def _handle_card_clicked(self, item) -> None:
        if self._click_action == "open":
            self.item_open_requested.emit(item)
            self.open_requested.emit(item.vod_id)
            return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_douban_page_ui.py::test_douban_page_clicking_card_emits_item_open_requested`
Expected: PASS and existing open-signal tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/ui/douban_page.py tests/test_douban_page_ui.py
git commit -m "feat: expose clicked emby card items"
```

### Task 3: Verify the full app surface

**Files:**
- Test: `tests/test_app.py`
- Test: `tests/test_douban_page_ui.py`
- Test: `tests/test_emby_controller.py`

- [ ] **Step 1: Run focused regression tests**

Run: `uv run pytest -q tests/test_app.py tests/test_douban_page_ui.py tests/test_emby_controller.py`
Expected: PASS

- [ ] **Step 2: Run full suite**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-04-16-emby-folder-navigation-design.md docs/superpowers/plans/2026-04-16-emby-folder-navigation.md
git commit -m "docs: add emby folder navigation plan"
```
