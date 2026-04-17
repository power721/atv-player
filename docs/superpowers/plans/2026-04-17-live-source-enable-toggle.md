# Live Source Enable Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `启用/禁用` action to live source management so the default `IPTV` source can be disabled without being deleted.

**Architecture:** Reuse the existing `enabled` field in `live_source` and the existing `CustomLiveService.load_categories()` filter. Add a small toggle method in the repository/service layer, then expose it through a new dialog button that reloads the table after each toggle.

**Tech Stack:** Python 3, SQLite, PySide6, pytest

---

## File Structure

### Modified Files

- `src/atv_player/live_source_repository.py`
  Add a small source-enabled toggle helper.
- `src/atv_player/custom_live_service.py`
  Expose a toggle method that delegates to the repository.
- `src/atv_player/ui/live_source_manager_dialog.py`
  Add a `启用/禁用` button and wire it to the selected source.
- `tests/test_live_source_manager_dialog.py`
  Assert toggling enabled state calls the manager with the correct boolean.

## Task 1: Add Source Toggle Support

**Files:**
- Modify: `src/atv_player/live_source_repository.py`
- Modify: `src/atv_player/custom_live_service.py`
- Test: `tests/test_live_source_manager_dialog.py`

- [ ] **Step 1: Write the failing dialog tests**

```python
def test_live_source_manager_dialog_toggle_disables_enabled_source(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.source_table.selectRow(0)

    dialog._toggle_selected_enabled()

    assert manager.toggle_calls == [(1, False)]


def test_live_source_manager_dialog_toggle_enables_disabled_source(qtbot) -> None:
    manager = FakeLiveSourceManager()
    manager.sources[0].enabled = False
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.source_table.selectRow(0)

    dialog._toggle_selected_enabled()

    assert manager.toggle_calls == [(1, True)]
```

- [ ] **Step 2: Run the focused dialog tests to verify they fail**

Run: `uv run pytest tests/test_live_source_manager_dialog.py -v`

Expected: FAIL with `AttributeError` because the dialog does not yet have `_toggle_selected_enabled`

- [ ] **Step 3: Add the repository and service toggle helpers**

```python
class LiveSourceRepository:
    ...
    def set_source_enabled(self, source_id: int, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE live_source SET enabled = ? WHERE id = ?",
                (int(enabled), source_id),
            )
```

```python
class CustomLiveService:
    ...
    def set_source_enabled(self, source_id: int, enabled: bool) -> None:
        self._repository.set_source_enabled(source_id, enabled)
```

- [ ] **Step 4: Run the existing focused repository and service behavior tests**

Run: `uv run pytest tests/test_live_source_repository.py tests/test_custom_live_service.py -v`

Expected: PASS without needing broader changes because category filtering already uses `enabled`

- [ ] **Step 5: Commit the toggle backend**

```bash
git add src/atv_player/live_source_repository.py src/atv_player/custom_live_service.py
git commit -m "feat: add live source enable toggle support"
```

## Task 2: Add The Dialog Toggle Button

**Files:**
- Modify: `src/atv_player/ui/live_source_manager_dialog.py`
- Modify: `tests/test_live_source_manager_dialog.py`

- [ ] **Step 1: Add the `启用/禁用` button to the dialog actions**

```python
self.toggle_button = QPushButton("启用/禁用")

for button in (
    self.add_remote_button,
    self.add_local_button,
    self.add_manual_button,
    self.toggle_button,
    self.manage_channels_button,
    self.refresh_button,
):
    actions.addWidget(button)
```

- [ ] **Step 2: Implement the toggle action**

```python
self.toggle_button.clicked.connect(self._toggle_selected_enabled)

def _toggle_selected_enabled(self) -> None:
    source_id = self._selected_source_id()
    if source_id is None:
        return
    row = self.source_table.currentRow()
    enabled_text = self.source_table.item(row, 3).text()
    self.manager.set_source_enabled(source_id, enabled_text != "是")
    self.reload_sources()
```

- [ ] **Step 3: Update the fake manager in tests**

```python
class FakeLiveSourceManager:
    def __init__(self) -> None:
        ...
        self.toggle_calls = []

    def set_source_enabled(self, source_id: int, enabled: bool):
        self.toggle_calls.append((source_id, enabled))
```

- [ ] **Step 4: Run the focused dialog tests to verify they pass**

Run: `uv run pytest tests/test_live_source_manager_dialog.py -v`

Expected: PASS including both new toggle tests

- [ ] **Step 5: Run the final focused verification**

Run: `uv run pytest tests/test_live_source_repository.py tests/test_custom_live_service.py tests/test_live_source_manager_dialog.py -v`

Expected: PASS with disabled sources still filtered out by existing service behavior

- [ ] **Step 6: Commit the dialog toggle**

```bash
git add src/atv_player/ui/live_source_manager_dialog.py tests/test_live_source_manager_dialog.py
git commit -m "feat: add live source toggle action"
```

## Self-Review

- Spec coverage:
  The new toggle button is covered by Task 2.
  Repository and service enable toggling are covered by Task 1.
  Disabled-source hiding remains covered by the existing `enabled` filter in `CustomLiveService`.
- Placeholder scan:
  No placeholders remain.
- Type consistency:
  `set_source_enabled` is used consistently in repository, service, and dialog tasks.
