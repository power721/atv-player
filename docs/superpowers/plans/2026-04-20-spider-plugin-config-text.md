# Spider Plugin Config Text Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist one raw config text per spider plugin, expose it in plugin management, and pass it into `Spider.init(config_text)` when loading spiders.

**Architecture:** Extend the existing `SpiderPluginConfig` and `SpiderPluginRepository` instead of creating a new table, then thread the new `config_text` field through `SpiderPluginManager`, `SpiderPluginLoader`, and `PluginManagerDialog`. Keep config editing as a dedicated dialog action so the plugin list table remains compact and operationally focused.

**Tech Stack:** Python 3.13, SQLite, PySide6, pytest, httpx

---

### Task 1: Persist Raw Plugin Config Text

**Files:**
- Modify: `src/atv_player/models.py:141-152`
- Modify: `src/atv_player/plugins/repository.py:22-151`
- Test: `tests/test_storage.py:243-443`

- [ ] **Step 1: Write the failing storage tests**

Add these tests near the existing spider plugin repository coverage in `tests/test_storage.py`:

```python
def test_spider_plugin_repository_round_trip_and_logs(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SpiderPluginRepository(db_path)

    local_plugin = repo.add_plugin(
        source_type="local",
        source_value="/plugins/红果短剧.py",
        display_name="红果短剧",
    )
    remote_plugin = repo.add_plugin(
        source_type="remote",
        source_value="https://example.com/spiders/hg.py",
        display_name="红果短剧远程",
    )

    assert local_plugin.config_text == ""
    assert remote_plugin.config_text == ""

    repo.update_plugin(
        local_plugin.id,
        display_name="红果短剧本地",
        enabled=False,
        cached_file_path="",
        last_loaded_at=1713206400,
        last_error="缺少依赖: pyquery",
        config_text="site=https://example.com\ncookie=abc",
    )
    repo.append_log(local_plugin.id, "error", "缺少依赖: pyquery", created_at=1713206401)
    repo.move_plugin(remote_plugin.id, direction=-1)

    plugins = repo.list_plugins()
    logs = repo.list_logs(local_plugin.id)

    assert [(item.display_name, item.sort_order, item.enabled) for item in plugins] == [
        ("红果短剧远程", 0, True),
        ("红果短剧本地", 1, False),
    ]
    assert plugins[1].last_error == "缺少依赖: pyquery"
    assert plugins[1].config_text == "site=https://example.com\ncookie=abc"
    assert logs[0].message == "缺少依赖: pyquery"

    repo.delete_plugin(remote_plugin.id)

    assert [item.display_name for item in repo.list_plugins()] == ["红果短剧本地"]


def test_spider_plugin_repository_migrates_missing_config_text_column(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE spider_plugins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_value TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL,
                cached_file_path TEXT NOT NULL DEFAULT '',
                last_loaded_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            INSERT INTO spider_plugins (
                source_type, source_value, display_name, enabled, sort_order,
                cached_file_path, last_loaded_at, last_error
            )
            VALUES ('local', '/plugins/红果短剧.py', '红果短剧', 1, 0, '', 0, '')
            """
        )

    repo = SpiderPluginRepository(db_path)
    plugin = repo.get_plugin(1)

    assert plugin.display_name == "红果短剧"
    assert plugin.config_text == ""
    repo.update_plugin(
        plugin.id,
        display_name=plugin.display_name,
        enabled=plugin.enabled,
        cached_file_path=plugin.cached_file_path,
        last_loaded_at=plugin.last_loaded_at,
        last_error=plugin.last_error,
        config_text="token=updated",
    )
    assert repo.get_plugin(1).config_text == "token=updated"
```

- [ ] **Step 2: Run the storage tests to verify they fail**

Run: `uv run pytest tests/test_storage.py -k "config_text or round_trip_and_logs" -v`

Expected: FAIL because `SpiderPluginConfig` has no `config_text` field yet and `SpiderPluginRepository.update_plugin()` does not accept a `config_text` argument.

- [ ] **Step 3: Write the minimal model and repository implementation**

Update `src/atv_player/models.py` so `SpiderPluginConfig` carries the new field at the end of the dataclass:

```python
@dataclass(slots=True)
class SpiderPluginConfig:
    id: int = 0
    source_type: str = ""
    source_value: str = ""
    display_name: str = ""
    enabled: bool = True
    sort_order: int = 0
    cached_file_path: str = ""
    last_loaded_at: int = 0
    last_error: str = ""
    config_text: str = ""
```

Update `src/atv_player/plugins/repository.py` to add the column, migrate old databases, read it, and write it:

```python
def _init_db(self) -> None:
    with self._connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spider_plugins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_value TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL,
                cached_file_path TEXT NOT NULL DEFAULT '',
                last_loaded_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                config_text TEXT NOT NULL DEFAULT ''
            )
            """
        )
        plugin_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(spider_plugins)").fetchall()
        }
        if "config_text" not in plugin_columns:
            conn.execute(
                "ALTER TABLE spider_plugins ADD COLUMN config_text TEXT NOT NULL DEFAULT ''"
            )
```

```python
def add_plugin(self, source_type: str, source_value: str, display_name: str) -> SpiderPluginConfig:
    with self._connect() as conn:
        next_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM spider_plugins"
        ).fetchone()[0]
        cursor = conn.execute(
            """
            INSERT INTO spider_plugins (
                source_type, source_value, display_name, enabled, sort_order,
                cached_file_path, last_loaded_at, last_error, config_text
            )
            VALUES (?, ?, ?, 1, ?, '', 0, '', '')
            """,
            (source_type, source_value, display_name, next_order),
        )
    return self.get_plugin(_require_lastrowid(cursor))
```

```python
SELECT id, source_type, source_value, display_name, enabled, sort_order,
       cached_file_path, last_loaded_at, last_error, config_text
FROM spider_plugins
```

```python
def update_plugin(
    self,
    plugin_id: int,
    *,
    display_name: str,
    enabled: bool,
    cached_file_path: str,
    last_loaded_at: int,
    last_error: str,
    config_text: str,
) -> None:
    with self._connect() as conn:
        conn.execute(
            """
            UPDATE spider_plugins
            SET display_name = ?, enabled = ?, cached_file_path = ?,
                last_loaded_at = ?, last_error = ?, config_text = ?
            WHERE id = ?
            """,
            (
                display_name,
                int(enabled),
                cached_file_path,
                last_loaded_at,
                last_error,
                config_text,
                plugin_id,
            ),
        )
```

- [ ] **Step 4: Run the storage tests to verify they pass**

Run: `uv run pytest tests/test_storage.py -k "config_text or round_trip_and_logs" -v`

Expected: PASS for the updated repository round-trip test and the new migration test.

- [ ] **Step 5: Commit the persistence changes**

```bash
git add tests/test_storage.py src/atv_player/models.py src/atv_player/plugins/repository.py
git commit -m "feat: store spider plugin config text"
```

### Task 2: Preserve and Edit Config Text Through the Plugin Manager Service

**Files:**
- Modify: `src/atv_player/plugins/__init__.py:37-148`
- Test: `tests/test_spider_plugin_manager.py:9-139`

- [ ] **Step 1: Write the failing manager test**

Add this test to `tests/test_spider_plugin_manager.py`:

```python
def test_manager_set_plugin_config_persists_raw_text_and_survives_other_updates(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")
    manager = SpiderPluginManager(repository, FakeLoader())

    manager.set_plugin_config(plugin.id, "token=abc\ncookie = 1\n")
    manager.rename_plugin(plugin.id, "红果短剧新版")
    manager.refresh_plugin(plugin.id)

    saved = repository.get_plugin(plugin.id)

    assert saved.display_name == "红果短剧新版"
    assert saved.config_text == "token=abc\ncookie = 1\n"
```

- [ ] **Step 2: Run the manager test to verify it fails**

Run: `uv run pytest tests/test_spider_plugin_manager.py::test_manager_set_plugin_config_persists_raw_text_and_survives_other_updates -v`

Expected: FAIL because `SpiderPluginManager` does not expose `set_plugin_config()` yet, and existing update paths do not pass `config_text` through to the repository.

- [ ] **Step 3: Write the minimal manager implementation**

Update `src/atv_player/plugins/__init__.py` so every repository update preserves `plugin.config_text`, and add a dedicated config setter:

```python
def rename_plugin(self, plugin_id: int, display_name: str) -> None:
    plugin = self._repository.get_plugin(plugin_id)
    self._repository.update_plugin(
        plugin_id,
        display_name=display_name,
        enabled=plugin.enabled,
        cached_file_path=plugin.cached_file_path,
        last_loaded_at=plugin.last_loaded_at,
        last_error=plugin.last_error,
        config_text=plugin.config_text,
    )


def set_plugin_config(self, plugin_id: int, config_text: str) -> None:
    plugin = self._repository.get_plugin(plugin_id)
    self._repository.update_plugin(
        plugin_id,
        display_name=plugin.display_name,
        enabled=plugin.enabled,
        cached_file_path=plugin.cached_file_path,
        last_loaded_at=plugin.last_loaded_at,
        last_error=plugin.last_error,
        config_text=config_text,
    )
```

Also update the other `update_plugin()` call sites in this file in the same way:

```python
config_text=plugin.config_text,
```

That line needs to be added in:

- `set_plugin_enabled()`
- both branches of `refresh_plugin()`
- the error branch inside `load_enabled_plugins()`

- [ ] **Step 4: Run the manager test to verify it passes**

Run: `uv run pytest tests/test_spider_plugin_manager.py::test_manager_set_plugin_config_persists_raw_text_and_survives_other_updates -v`

Expected: PASS with the saved raw config text unchanged after rename and refresh.

- [ ] **Step 5: Commit the manager changes**

```bash
git add tests/test_spider_plugin_manager.py src/atv_player/plugins/__init__.py
git commit -m "feat: manage spider plugin config text"
```

### Task 3: Initialize Spiders With Saved Config Text

**Files:**
- Modify: `src/atv_player/plugins/loader.py:30-69`
- Test: `tests/test_spider_plugin_loader.py:27-203`

- [ ] **Step 1: Write the failing loader test**

Add this test to `tests/test_spider_plugin_loader.py`:

```python
def test_loader_passes_saved_config_text_into_spider_init(tmp_path: Path) -> None:
    plugin_path = tmp_path / "config_plugin.py"
    plugin_path.write_text(
        """
from base.spider import Spider

class Spider(Spider):
    def init(self, extend=""):
        self.extend = extend

    def getName(self):
        return self.extend
""",
        encoding="utf-8",
    )
    loader = SpiderPluginLoader(cache_dir=tmp_path / "cache")
    config = SpiderPluginConfig(
        id=21,
        source_type="local",
        source_value=str(plugin_path),
        display_name="",
        enabled=True,
        sort_order=0,
        config_text="site=https://example.com\ncookie=abc",
    )

    loaded = loader.load(config)

    assert loaded.plugin_name == "site=https://example.com\ncookie=abc"
    assert loaded.config.config_text == "site=https://example.com\ncookie=abc"
```

- [ ] **Step 2: Run the loader test to verify it fails**

Run: `uv run pytest tests/test_spider_plugin_loader.py::test_loader_passes_saved_config_text_into_spider_init -v`

Expected: FAIL because the loader currently calls `spider.init("")` and drops `config_text` when creating the updated config snapshot.

- [ ] **Step 3: Write the minimal loader implementation**

Update `src/atv_player/plugins/loader.py` to pass the stored text into `init` and preserve it in the returned config:

```python
spider = spider_cls()
if hasattr(spider, "init"):
    spider.init(config.config_text)
plugin_name = str(getattr(spider, "getName", lambda: "")() or "")
```

```python
updated_config = SpiderPluginConfig(
    id=config.id,
    source_type=config.source_type,
    source_value=config.source_value,
    display_name=config.display_name,
    enabled=config.enabled,
    sort_order=config.sort_order,
    cached_file_path=str(source_path) if config.source_type == "remote" else config.cached_file_path,
    last_loaded_at=config.last_loaded_at,
    last_error=config.last_error,
    config_text=config.config_text,
)
```

- [ ] **Step 4: Run the loader test to verify it passes**

Run: `uv run pytest tests/test_spider_plugin_loader.py::test_loader_passes_saved_config_text_into_spider_init -v`

Expected: PASS with `Spider.getName()` reflecting the raw config text received during `init`.

- [ ] **Step 5: Commit the loader changes**

```bash
git add tests/test_spider_plugin_loader.py src/atv_player/plugins/loader.py
git commit -m "feat: initialize spiders with saved config text"
```

### Task 4: Add Config Editing to the Plugin Manager Dialog

**Files:**
- Modify: `src/atv_player/ui/plugin_manager_dialog.py:28-231`
- Test: `tests/test_plugin_manager_dialog.py:7-164`

- [ ] **Step 1: Write the failing dialog tests**

Update `tests/test_plugin_manager_dialog.py` with these changes:

```python
class FakePluginManager:
    def __init__(self) -> None:
        self.plugins = [
            SpiderPluginConfig(
                id=1,
                source_type="local",
                source_value="/plugins/a.py",
                display_name="本地A",
                enabled=True,
                sort_order=0,
                config_text="token=local",
            ),
            SpiderPluginConfig(
                id=2,
                source_type="remote",
                source_value="https://example.com/b.py",
                display_name="远程B",
                enabled=False,
                sort_order=1,
                last_error="下载失败",
                config_text="token=remote\ncookie=1\n",
            ),
        ]
        self.logs = {
            2: [SpiderPluginLogEntry(id=1, plugin_id=2, level="error", message="下载失败", created_at=1713206400)]
        }
        self.rename_calls: list[tuple[int, str]] = []
        self.config_calls: list[tuple[int, str]] = []
        self.toggle_calls: list[tuple[int, bool]] = []
        self.move_calls: list[tuple[int, int]] = []
        self.refresh_calls: list[int] = []
        self.add_local_calls: list[str] = []
        self.add_remote_calls: list[str] = []
        self.delete_calls: list[int] = []

    def set_plugin_config(self, plugin_id: int, config_text: str) -> None:
        self.config_calls.append((plugin_id, config_text))
```

```python
def test_plugin_manager_dialog_disables_row_actions_without_selection(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.plugin_table.clearSelection()

    assert dialog.rename_button.isEnabled() is False
    assert dialog.config_button.isEnabled() is False
    assert dialog.toggle_button.isEnabled() is False
    assert dialog.up_button.isEnabled() is False
    assert dialog.down_button.isEnabled() is False
    assert dialog.refresh_button.isEnabled() is False
    assert dialog.logs_button.isEnabled() is False
    assert dialog.delete_button.isEnabled() is False
```

```python
def test_plugin_manager_dialog_edit_config_allows_empty_string_and_keeps_raw_current_value(
    qtbot,
    monkeypatch,
) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.plugin_table.selectRow(1)

    captured: list[str] = []

    def fake_prompt(current: str) -> str | None:
        captured.append(current)
        return ""

    monkeypatch.setattr(dialog, "_prompt_config_text", fake_prompt)

    dialog._edit_selected_config()

    assert captured == ["token=remote\ncookie=1\n"]
    assert manager.config_calls == [(2, "")]
```

- [ ] **Step 2: Run the dialog tests to verify they fail**

Run: `uv run pytest tests/test_plugin_manager_dialog.py -k "config or disables_row_actions_without_selection" -v`

Expected: FAIL because the dialog has no `config_button`, no `_prompt_config_text()` helper, and no `_edit_selected_config()` action yet.

- [ ] **Step 3: Write the minimal dialog implementation**

Update `src/atv_player/ui/plugin_manager_dialog.py` to add a dedicated config button, a multiline prompt, and an edit action that distinguishes cancel from saving an empty string:

```python
self.config_button = QPushButton("编辑配置")
```

Insert the new button into the action row immediately after `self.rename_button`, connect it, and enable or disable it with the other row actions:

```python
self.config_button.clicked.connect(self._edit_selected_config)
```

```python
self.config_button.setEnabled(has_selection)
```

Add a multiline prompt helper:

```python
def _prompt_config_text(self, current: str) -> str | None:
    value, accepted = QInputDialog.getMultiLineText(self, "编辑配置", "配置文本", current)
    return value if accepted else None
```

Add the edit action:

```python
def _edit_selected_config(self) -> None:
    plugin_id = self._selected_plugin_id()
    if plugin_id is None:
        return
    plugin = next((item for item in self.plugin_manager.list_plugins() if item.id == plugin_id), None)
    if plugin is None:
        return
    config_text = self._prompt_config_text(plugin.config_text)
    if config_text is None:
        return
    self.plugin_manager.set_plugin_config(plugin_id, config_text)
    self.reload_plugins()
```

- [ ] **Step 4: Run the dialog tests to verify they pass**

Run: `uv run pytest tests/test_plugin_manager_dialog.py -k "config or disables_row_actions_without_selection" -v`

Expected: PASS with the config button disabled when nothing is selected and empty-string config saves treated as valid.

- [ ] **Step 5: Commit the dialog changes**

```bash
git add tests/test_plugin_manager_dialog.py src/atv_player/ui/plugin_manager_dialog.py
git commit -m "feat: edit spider plugin config text in manager"
```

### Task 5: Run the Focused Verification Suite

**Files:**
- Verify: `tests/test_storage.py`
- Verify: `tests/test_spider_plugin_manager.py`
- Verify: `tests/test_spider_plugin_loader.py`
- Verify: `tests/test_plugin_manager_dialog.py`

- [ ] **Step 1: Run the focused regression suite**

Run: `uv run pytest tests/test_storage.py tests/test_spider_plugin_manager.py tests/test_spider_plugin_loader.py tests/test_plugin_manager_dialog.py -v`

Expected: PASS for all spider plugin persistence, manager, loader, and dialog coverage.

- [ ] **Step 2: Inspect the working tree**

Run: `git status --short`

Expected: only the intended source, test, and plan/spec files are modified or added.

- [ ] **Step 3: Summarize verification evidence before handoff**

Record the exact pytest command that passed and any intentionally unrun suites so the final handoff does not claim broader coverage than was actually executed.
