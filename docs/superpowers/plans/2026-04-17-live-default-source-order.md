# Live Default Source Naming And Ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the default live source to `IPTV` and append custom live categories to the end of the `网络直播` category list.

**Architecture:** Keep the change narrowly scoped to one repository constant and one controller ordering rule. Verify behavior through focused repository and controller tests without changing schema or the internal sort order of custom sources.

**Tech Stack:** Python 3, pytest, existing SQLite repository and live controller code

---

## File Structure

### Modified Files

- `src/atv_player/live_source_repository.py`
  Update the default source display name constant.
- `src/atv_player/controllers/live_controller.py`
  Change category ordering so custom categories append after backend categories.
- `tests/test_live_source_repository.py`
  Assert the default source name is `IPTV`.
- `tests/test_live_controller.py`
  Assert custom categories are appended after backend categories.

## Task 1: Update Default Source Naming

**Files:**
- Modify: `src/atv_player/live_source_repository.py`
- Test: `tests/test_live_source_repository.py`

- [ ] **Step 1: Write the failing repository test**

```python
def test_live_source_repository_inserts_default_example_source(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")

    sources = repo.list_sources()

    assert len(sources) == 1
    assert sources[0].display_name == "IPTV"
```

- [ ] **Step 2: Run the focused repository test to verify it fails**

Run: `uv run pytest tests/test_live_source_repository.py::test_live_source_repository_inserts_default_example_source -v`

Expected: FAIL because the default name is still `示例直播源`

- [ ] **Step 3: Update the default name constant**

```python
_DEFAULT_SOURCE_NAME = "IPTV"
```

- [ ] **Step 4: Run the repository tests to verify they pass**

Run: `uv run pytest tests/test_live_source_repository.py -v`

Expected: PASS for all repository tests

- [ ] **Step 5: Commit the default-name change**

```bash
git add src/atv_player/live_source_repository.py tests/test_live_source_repository.py
git commit -m "fix: rename default live source"
```

## Task 2: Move Custom Live Categories To The End

**Files:**
- Modify: `src/atv_player/controllers/live_controller.py`
- Test: `tests/test_live_controller.py`

- [ ] **Step 1: Write the failing controller test**

```python
def test_load_categories_appends_enabled_custom_sources() -> None:
    from atv_player.controllers.live_controller import LiveController

    api = FakeApiClient()
    api.category_payload = {"class": [{"type_id": "bili", "type_name": "哔哩哔哩"}]}

    class FakeCustomService:
        def load_categories(self):
            return [DoubanCategory(type_id="custom:7", type_name="自定义远程")]

    controller = LiveController(api, custom_live_service=FakeCustomService())

    categories = controller.load_categories()

    assert [(item.type_id, item.type_name) for item in categories] == [
        ("0", "推荐"),
        ("bili", "哔哩哔哩"),
        ("custom:7", "自定义远程"),
    ]
```

- [ ] **Step 2: Run the focused controller test to verify it fails**

Run: `uv run pytest tests/test_live_controller.py::test_load_categories_appends_enabled_custom_sources -v`

Expected: FAIL because custom categories are still prepended

- [ ] **Step 3: Update the category ordering in `LiveController.load_categories()`**

```python
def load_categories(self) -> list[DoubanCategory]:
    payload = self._api_client.list_live_categories()
    categories = [_map_category(item) for item in payload.get("class", [])]
    categories = [category for category in categories if category.type_id != "0"]
    custom_categories: list[DoubanCategory] = []
    if self._custom_live_service is not None:
        custom_categories = list(self._custom_live_service.load_categories())
    return [DoubanCategory(type_id="0", type_name="推荐"), *categories, *custom_categories]
```

- [ ] **Step 4: Run the focused controller tests to verify they pass**

Run: `uv run pytest tests/test_live_controller.py -v`

Expected: PASS for existing live controller tests and the updated ordering test

- [ ] **Step 5: Commit the ordering change**

```bash
git add src/atv_player/controllers/live_controller.py tests/test_live_controller.py
git commit -m "fix: append custom live categories"
```

## Task 3: Final Verification

**Files:**
- Modify: `src/atv_player/ui/live_source_manager_dialog.py`
- Test: `tests/test_live_source_manager_dialog.py`

- [ ] **Step 1: Verify the related focused suites together**

Run: `uv run pytest tests/test_live_source_repository.py tests/test_live_controller.py tests/test_live_source_manager_dialog.py -v`

Expected: PASS with the default name and category order updated

- [ ] **Step 2: Commit any related UI text updates if present**

```bash
git add src/atv_player/ui/live_source_manager_dialog.py tests/test_live_source_manager_dialog.py
git commit -m "fix: align live source labels"
```

## Self-Review

- Spec coverage:
  The default source naming change is covered by Task 1.
  The category ordering change is covered by Task 2.
- Placeholder scan:
  No placeholders remain.
- Type consistency:
  The plan uses the existing `LiveController`, `LiveSourceRepository`, and focused test names consistently.
