# GitHub Plugin Import Cancel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cancellable GitHub repository import to the plugin manager so users can stop future imports while keeping already completed plugin additions and updates.

**Architecture:** Keep cancellation policy inside `SpiderPluginManager` by adding a plain Python `cancel_callback` plus a dedicated `SpiderPluginImportCancelled` exception that carries partial counts. Keep `PluginManagerDialog` responsible for surfacing the cancel button, passing `progress.wasCanceled`, reloading the plugin list after success or cancel, and presenting the correct summary message.

**Tech Stack:** Python 3.13, PySide6, httpx, sqlite-backed plugin repository, pytest

---

## File Structure

- Modify: `src/atv_player/models.py`
  Add a dedicated import-cancel exception next to the existing GitHub import progress/result models.
- Modify: `src/atv_player/plugins/__init__.py`
  Extend `SpiderPluginManager.import_github_repository(...)` with cancellation checks and partial-result propagation.
- Modify: `src/atv_player/ui/plugin_manager_dialog.py`
  Keep the modal progress dialog, restore the cancel button, pass `wasCanceled()`, and show a cancel summary instead of an error.
- Modify: `tests/test_spider_plugin_manager.py`
  Lock down manager-side cancellation semantics first.
- Modify: `tests/test_plugin_manager_dialog.py`
  Lock down the dialog cancel button, cancel summary, and list reload behavior.

### Task 1: Lock Manager Cancellation With Failing Tests

**Files:**
- Modify: `tests/test_spider_plugin_manager.py:8-14`
- Modify: `tests/test_spider_plugin_manager.py:204-338`
- Test: `tests/test_spider_plugin_manager.py`

- [ ] **Step 1: Write the failing manager cancellation test**

Update the import block near the top of `tests/test_spider_plugin_manager.py` so it includes the new exception:

```python
from atv_player.models import (
    SpiderPluginAction,
    SpiderPluginConfig,
    SpiderPluginImportCancelled,
    SpiderPluginImportProgress,
    SpiderPluginImportResult,
)
```

Add this test immediately after `test_manager_import_github_repository_skips_same_version_and_updates_existing_version(...)`:

```python
def test_manager_import_github_repository_stops_after_cancellation_and_preserves_completed_changes(
    tmp_path: Path,
) -> None:
    first_url = "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E6%BD%AE%E6%B5%81APP.txt"
    second_url = "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E5%8F%8C%E6%98%9F.txt"
    responses = {
        "https://api.github.com/repos/har01d5/tvbox": httpx.Response(
            200,
            json={"default_branch": "master"},
        ),
        "https://raw.githubusercontent.com/har01d5/tvbox/master/spiders_v2.json": httpx.Response(
            200,
            json=[
                {"file": "py/潮流APP.txt", "valid": True},
                {"file": "py/双星.txt", "valid": True},
            ],
        ),
        first_url: httpx.Response(
            200,
            text="//@version:6\nprint('a')\n",
        ),
        second_url: httpx.Response(
            200,
            text="//@version:7\nprint('b')\n",
        ),
    }

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        response = responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response

    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader(), get=fake_get)

    def cancel_callback() -> bool:
        return repository.find_plugin_by_source_value(first_url) is not None

    with pytest.raises(SpiderPluginImportCancelled) as exc_info:
        manager.import_github_repository(
            "https://github.com/har01d5/tvbox",
            cancel_callback=cancel_callback,
        )

    plugins = repository.list_plugins()

    assert exc_info.value.result == SpiderPluginImportResult(
        imported_count=1,
        updated_count=0,
        skipped_count=0,
    )
    assert [plugin.source_value for plugin in plugins] == [first_url]
    assert plugins[0].plugin_version == 6
```

- [ ] **Step 2: Run the targeted manager tests to verify the new behavior is missing**

Run:

```bash
uv run pytest tests/test_spider_plugin_manager.py -k "import_github_repository" -v
```

Expected: FAIL because `SpiderPluginImportCancelled` does not exist yet and `SpiderPluginManager.import_github_repository(...)` does not accept `cancel_callback`.

- [ ] **Step 3: Commit the red manager test**

Run:

```bash
git add tests/test_spider_plugin_manager.py
git commit -m "test: cover github import cancellation in manager"
```

### Task 2: Implement Manager Cancellation And Partial Results

**Files:**
- Modify: `src/atv_player/models.py:296-308`
- Modify: `src/atv_player/plugins/__init__.py:11-18`
- Modify: `src/atv_player/plugins/__init__.py:175-176`
- Modify: `src/atv_player/plugins/__init__.py:351-414`
- Test: `tests/test_spider_plugin_manager.py`

- [ ] **Step 1: Add the dedicated import-cancel exception model**

In `src/atv_player/models.py`, insert this class right after `SpiderPluginImportResult`:

```python
class SpiderPluginImportCancelled(Exception):
    def __init__(self, result: SpiderPluginImportResult) -> None:
        super().__init__("已取消导入")
        self.result = result
```

- [ ] **Step 2: Implement cancellation checks in `SpiderPluginManager`**

Update the imports near the top of `src/atv_player/plugins/__init__.py`:

```python
from atv_player.models import (
    SpiderPluginAction,
    SpiderPluginActionContext,
    SpiderPluginConfig,
    SpiderPluginImportCancelled,
    SpiderPluginImportProgress,
    SpiderPluginImportResult,
)
```

Add this helper method inside `SpiderPluginManager`, near `_emit_import_progress(...)`:

```python
    def _raise_if_import_cancelled(
        self,
        cancel_callback: Callable[[], bool] | None,
        result: SpiderPluginImportResult,
    ) -> None:
        if cancel_callback is not None and cancel_callback():
            raise SpiderPluginImportCancelled(result)
```

Then replace `import_github_repository(...)` with this version:

```python
    def import_github_repository(
        self,
        repo_url: str,
        *,
        progress_callback: Callable[[SpiderPluginImportProgress], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> SpiderPluginImportResult:
        result = SpiderPluginImportResult()
        owner, repo = _parse_github_repo(repo_url)
        self._raise_if_import_cancelled(cancel_callback, result)
        self._emit_import_progress(progress_callback, stage="resolve_repo", message="正在解析仓库信息")
        self._raise_if_import_cancelled(cancel_callback, result)
        default_branch = self._load_github_default_branch(owner, repo)
        self._emit_import_progress(progress_callback, stage="fetch_manifest", message="正在读取 spiders_v2.json")
        self._raise_if_import_cancelled(cancel_callback, result)
        manifest = self._fetch_json(_raw_github_url(owner, repo, default_branch, "spiders_v2.json"))
        if not isinstance(manifest, list):
            raise ValueError("spiders_v2.json 格式无效")

        valid_entries = [entry for entry in manifest if isinstance(entry, dict) and str(entry.get("file") or "").strip()]
        total = len(valid_entries)
        for index, entry in enumerate(valid_entries, start=1):
            self._raise_if_import_cancelled(cancel_callback, result)
            file_path = str(entry.get("file") or "").strip()
            self._emit_import_progress(
                progress_callback,
                stage="import_plugin",
                current=index,
                total=total,
                message=f"正在导入 {file_path}",
            )
            path = PurePosixPath(file_path)
            if path.is_absolute() or ".." in path.parts:
                result.skipped_count += 1
                continue
            try:
                source_url = _raw_github_url(owner, repo, default_branch, file_path)
                self._raise_if_import_cancelled(cancel_callback, result)
                source_text = self._fetch_text(source_url)
                plugin_version = _parse_plugin_version(source_text)
                existing = self._repository.find_plugin_by_source_value(source_url)
                if existing is None:
                    plugin = self._repository.add_plugin(
                        "remote",
                        source_url,
                        _default_plugin_name("remote", source_url),
                        enabled=bool(entry.get("valid", True)),
                        plugin_version=plugin_version,
                    )
                    result.imported_count += 1
                    self._raise_if_import_cancelled(cancel_callback, result)
                    self.refresh_plugin(plugin.id)
                    continue
                if existing.plugin_version == plugin_version:
                    result.skipped_count += 1
                    continue
                self._repository.update_plugin(
                    existing.id,
                    display_name=existing.display_name,
                    enabled=existing.enabled,
                    cached_file_path=existing.cached_file_path,
                    last_loaded_at=existing.last_loaded_at,
                    last_error=existing.last_error,
                    config_text=existing.config_text,
                    plugin_version=plugin_version,
                )
                result.updated_count += 1
                self._raise_if_import_cancelled(cancel_callback, result)
                self.refresh_plugin(existing.id)
            except SpiderPluginImportCancelled:
                raise
            except Exception:
                result.skipped_count += 1
        return result
```

This ordering is deliberate: counts are updated before the post-write cancel check so a persisted add/update is still reflected in the partial summary.

- [ ] **Step 3: Run the focused manager tests to verify green**

Run:

```bash
uv run pytest tests/test_spider_plugin_manager.py -k "import_github_repository" -v
```

Expected: PASS for the existing GitHub import tests plus the new cancellation test.

- [ ] **Step 4: Commit the manager implementation**

Run:

```bash
git add src/atv_player/models.py src/atv_player/plugins/__init__.py tests/test_spider_plugin_manager.py
git commit -m "feat: support canceling github plugin imports"
```

### Task 3: Lock Dialog Cancellation UX With Failing Tests

**Files:**
- Modify: `tests/test_plugin_manager_dialog.py:6-15`
- Modify: `tests/test_plugin_manager_dialog.py:25-118`
- Modify: `tests/test_plugin_manager_dialog.py:391-510`
- Test: `tests/test_plugin_manager_dialog.py`

- [ ] **Step 1: Update the fake manager and add failing dialog cancellation tests**

First, update the model imports near the top of `tests/test_plugin_manager_dialog.py`:

```python
from atv_player.models import (
    SpiderPluginAction,
    SpiderPluginConfig,
    SpiderPluginImportCancelled,
    SpiderPluginImportProgress,
    SpiderPluginImportResult,
    SpiderPluginLogEntry,
)
```

Then extend `FakePluginManager` with cancel tracking:

```python
        self.cancel_callback_checks = 0
        self.cancel_result = SpiderPluginImportResult(imported_count=1, updated_count=0, skipped_count=0)

    def import_github_repository(self, repo_url: str, *, progress_callback=None, cancel_callback=None):
        self.github_import_calls.append(repo_url)
        if progress_callback is not None:
            for event in (
                SpiderPluginImportProgress(stage="resolve_repo", message="正在解析仓库信息"),
                SpiderPluginImportProgress(stage="fetch_manifest", message="正在读取 spiders_v2.json"),
                SpiderPluginImportProgress(stage="import_plugin", current=1, total=2, message="正在导入 py/a.txt"),
                SpiderPluginImportProgress(stage="import_plugin", current=2, total=2, message="正在导入 py/b.txt"),
            ):
                self.progress_events.append((event.stage, event.current, event.total, event.message))
                progress_callback(event)
                if cancel_callback is not None:
                    self.cancel_callback_checks += 1
                    if cancel_callback():
                        raise SpiderPluginImportCancelled(self.cancel_result)
        return self.github_import_result
```

Replace `test_plugin_manager_dialog_import_progress_is_modal_and_has_no_cancel_button(...)` with this test:

```python
def test_plugin_manager_dialog_import_progress_is_modal_and_keeps_cancel_button(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    captured: dict[str, object] = {}

    class FakeProgressDialog:
        def __init__(self, *args, **kwargs) -> None:
            captured["instance"] = self
            self.cancel_button = "present"
            self.window_modality = None

        def setWindowTitle(self, title: str) -> None:
            pass

        def setMinimumDuration(self, duration: int) -> None:
            pass

        def setAutoClose(self, auto_close: bool) -> None:
            pass

        def setAutoReset(self, auto_reset: bool) -> None:
            pass

        def setCancelButton(self, button) -> None:
            self.cancel_button = button

        def setWindowModality(self, modality) -> None:
            self.window_modality = modality

        def setRange(self, minimum: int, maximum: int) -> None:
            pass

        def setValue(self, value: int) -> None:
            pass

        def setLabelText(self, text: str) -> None:
            pass

        def wasCanceled(self) -> bool:
            return False

        def show(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QProgressDialog", FakeProgressDialog)
    monkeypatch.setattr(dialog, "_prompt_github_repo_url", lambda: "https://github.com/har01d5/tvbox")
    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QMessageBox.information", lambda *args: None)
    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QApplication.processEvents", lambda *args, **kwargs: None)

    dialog._import_github_repository()

    progress = captured["instance"]
    assert progress.cancel_button == "present"
    assert progress.window_modality == Qt.WindowModality.WindowModal
```

Add this new test after it:

```python
def test_plugin_manager_dialog_reports_cancelled_import_and_reloads_plugins(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    info_messages: list[str] = []
    warning_messages: list[str] = []
    reload_calls: list[str] = []
    original_reload = dialog.reload_plugins

    def tracked_reload() -> None:
        reload_calls.append("reload")
        original_reload()

    class FakeProgressDialog:
        def __init__(self, *args, **kwargs) -> None:
            self._cancelled = False

        def setWindowTitle(self, title: str) -> None:
            pass

        def setMinimumDuration(self, duration: int) -> None:
            pass

        def setAutoClose(self, auto_close: bool) -> None:
            pass

        def setAutoReset(self, auto_reset: bool) -> None:
            pass

        def setCancelButton(self, button) -> None:
            pass

        def setWindowModality(self, modality) -> None:
            pass

        def setRange(self, minimum: int, maximum: int) -> None:
            pass

        def setValue(self, value: int) -> None:
            pass

        def setLabelText(self, text: str) -> None:
            if text == "正在导入 py/a.txt":
                self._cancelled = True

        def wasCanceled(self) -> bool:
            return self._cancelled

        def show(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(dialog, "reload_plugins", tracked_reload)
    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QProgressDialog", FakeProgressDialog)
    monkeypatch.setattr(dialog, "_prompt_github_repo_url", lambda: "https://github.com/har01d5/tvbox")
    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QMessageBox.information", lambda *args: info_messages.append(args[2]))
    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QMessageBox.warning", lambda *args: warning_messages.append(args[2]))
    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QApplication.processEvents", lambda *args, **kwargs: None)

    dialog._import_github_repository()

    assert manager.github_import_calls == ["https://github.com/har01d5/tvbox"]
    assert manager.cancel_callback_checks > 0
    assert reload_calls == ["reload"]
    assert info_messages == ["已取消：新增 1 个，更新 0 个，跳过 0 个。"]
    assert warning_messages == []
```

- [ ] **Step 2: Run the targeted dialog tests to verify the behavior is still red**

Run:

```bash
uv run pytest tests/test_plugin_manager_dialog.py -k "github_repository or import_progress or cancelled_import" -v
```

Expected: FAIL because the dialog still removes the cancel button and does not catch `SpiderPluginImportCancelled`.

- [ ] **Step 3: Commit the red dialog tests**

Run:

```bash
git add tests/test_plugin_manager_dialog.py
git commit -m "test: cover github import cancellation in dialog"
```

### Task 4: Implement Dialog Cancellation Flow

**Files:**
- Modify: `src/atv_player/ui/plugin_manager_dialog.py:8-26`
- Modify: `src/atv_player/ui/plugin_manager_dialog.py:406-441`
- Test: `tests/test_plugin_manager_dialog.py`

- [ ] **Step 1: Catch the cancel exception and keep the progress dialog cancelable**

Update the imports in `src/atv_player/ui/plugin_manager_dialog.py`:

```python
from atv_player.models import SpiderPluginImportCancelled
```

Then replace `_import_github_repository(...)` with this implementation:

```python
    def _import_github_repository(self) -> None:
        if self._import_in_progress:
            return
        repo_url = self._prompt_github_repo_url()
        if not repo_url:
            return
        progress = QProgressDialog("", "", 0, 0, self)
        progress.setWindowTitle("从 GitHub 导入")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._import_in_progress = True
        self.import_github_button.setEnabled(False)
        progress.show()
        try:
            result = self.plugin_manager.import_github_repository(
                repo_url,
                progress_callback=lambda event: self._update_import_progress(progress, event),
                cancel_callback=lambda: progress.wasCanceled(),
            )
        except SpiderPluginImportCancelled as exc:
            result = exc.result
            if result.imported_count or result.updated_count:
                self.plugin_tabs_dirty = True
            self.reload_plugins()
            QMessageBox.information(
                self,
                "导入已取消",
                f"已取消：新增 {result.imported_count} 个，更新 {result.updated_count} 个，跳过 {result.skipped_count} 个。",
            )
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
        else:
            if result.imported_count or result.updated_count:
                self.plugin_tabs_dirty = True
            self.reload_plugins()
            QMessageBox.information(
                self,
                "导入完成",
                f"导入完成：新增 {result.imported_count} 个，更新 {result.updated_count} 个，跳过 {result.skipped_count} 个。",
            )
        finally:
            progress.close()
            self._import_in_progress = False
            self.import_github_button.setEnabled(True)
```

The important behavior changes here are:

- `setCancelButton(None)` is removed.
- `cancel_callback=lambda: progress.wasCanceled()` is passed through.
- cancel is treated as an informational completion path, not a warning path.
- `reload_plugins()` runs for both success and cancellation.

- [ ] **Step 2: Run the focused dialog tests to verify green**

Run:

```bash
uv run pytest tests/test_plugin_manager_dialog.py -k "github_repository or import_progress or cancelled_import" -v
```

Expected: PASS for the import summary test, the modal+cancel-button test, the re-entrant guard test, and the new cancelled-import test.

- [ ] **Step 3: Commit the dialog implementation**

Run:

```bash
git add src/atv_player/ui/plugin_manager_dialog.py tests/test_plugin_manager_dialog.py
git commit -m "feat: add cancelable github plugin import dialog"
```

### Task 5: Verify The Full Feature Slice

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/plugins/__init__.py`
- Modify: `src/atv_player/ui/plugin_manager_dialog.py`
- Modify: `tests/test_spider_plugin_manager.py`
- Modify: `tests/test_plugin_manager_dialog.py`
- Test: `tests/test_spider_plugin_manager.py`, `tests/test_plugin_manager_dialog.py`

- [ ] **Step 1: Run both focused test files together**

Run:

```bash
uv run pytest tests/test_spider_plugin_manager.py tests/test_plugin_manager_dialog.py -v
```

Expected: PASS across the GitHub import manager tests, dialog import tests, and the surrounding plugin-manager regressions.

- [ ] **Step 2: Review the final diff**

Run:

```bash
git diff -- src/atv_player/models.py src/atv_player/plugins/__init__.py src/atv_player/ui/plugin_manager_dialog.py tests/test_spider_plugin_manager.py tests/test_plugin_manager_dialog.py
```

Expected: Only the dedicated cancel exception, manager cancel plumbing, dialog cancel flow, and the new tests appear.

- [ ] **Step 3: Commit the verified feature**

Run:

```bash
git add src/atv_player/models.py src/atv_player/plugins/__init__.py src/atv_player/ui/plugin_manager_dialog.py tests/test_spider_plugin_manager.py tests/test_plugin_manager_dialog.py
git commit -m "feat: support canceling github plugin imports"
```
