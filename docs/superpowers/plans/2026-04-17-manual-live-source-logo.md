# Manual Live Source Logo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional `logo_url` support to manual live-source channels so manually created channels can show posters in browse cards and playback metadata.

**Architecture:** Extend the manual-channel storage model with an additive SQLite migration, then thread the new `logo_url` field through repository and service layers until it reaches the existing `vod_pic` flow. Update the manual-channel editor UI to collect and display the optional URL without adding preview or network validation logic.

**Tech Stack:** Python 3, SQLite, PySide6, pytest

---

## File Structure

### Modified Files

- `src/atv_player/models.py`
  Add `logo_url` to `LiveSourceEntry`.
- `src/atv_player/live_source_repository.py`
  Add the schema migration and include `logo_url` in manual-entry CRUD queries.
- `src/atv_player/custom_live_service.py`
  Copy stored `logo_url` into manual `ParsedChannel` objects.
- `src/atv_player/ui/manual_live_source_dialog.py`
  Add a `Logo URL` form field, a `Logo` table column, and pass the field through add/edit actions.
- `tests/test_live_source_repository.py`
  Cover additive migration plus manual-entry round-tripping for `logo_url`.
- `tests/test_custom_live_service.py`
  Cover `vod_pic` propagation for manual channels.
- `tests/test_live_source_manager_dialog.py`
  Cover add/edit dialog forwarding and table rendering for `logo_url`.

## Task 1: Add The Manual Channel Logo Field To Storage

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/live_source_repository.py`
- Test: `tests/test_live_source_repository.py`

- [ ] **Step 1: Write the failing repository tests**

```python
def test_live_source_repository_migrates_existing_manual_entry_table_with_logo_column(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE live_source (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_value TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                last_refreshed_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                cache_text TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE live_source_entry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                group_name TEXT NOT NULL DEFAULT '',
                channel_name TEXT NOT NULL,
                stream_url TEXT NOT NULL,
                sort_order INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO live_source (source_type, source_value, display_name, enabled, sort_order, is_default)
            VALUES ('manual', '', '手动源', 1, 0, 0)
            """
        )
        conn.execute(
            """
            INSERT INTO live_source_entry (source_id, group_name, channel_name, stream_url, sort_order)
            VALUES (1, '央视', 'CCTV-1', 'https://live.example/cctv1.m3u8', 0)
            """
        )

    repo = LiveSourceRepository(db_path)

    entry = repo.list_manual_entries(1)[0]

    assert entry.logo_url == ""


def test_live_source_repository_round_trips_manual_entry_logo_url(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("manual", "", "手动源")
    entry = repo.add_manual_entry(
        source.id,
        group_name="央视",
        channel_name="CCTV-1",
        stream_url="https://live.example/cctv1.m3u8",
        logo_url="https://img.example/cctv1.png",
    )

    repo.update_manual_entry(
        entry.id,
        group_name="央视频道",
        channel_name="CCTV-1综合",
        stream_url="https://live.example/cctv1hd.m3u8",
        logo_url="https://img.example/cctv1hd.png",
    )

    saved = repo.get_manual_entry(entry.id)

    assert saved.logo_url == "https://img.example/cctv1hd.png"
```

- [ ] **Step 2: Run the repository tests to verify they fail**

Run: `uv run pytest tests/test_live_source_repository.py -v`
Expected: FAIL because `LiveSourceEntry` and repository methods do not yet support `logo_url`

- [ ] **Step 3: Add the model field and repository migration**

```python
@dataclass(slots=True)
class LiveSourceEntry:
    id: int = 0
    source_id: int = 0
    group_name: str = ""
    channel_name: str = ""
    stream_url: str = ""
    logo_url: str = ""
    sort_order: int = 0
```

```python
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS live_source_entry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        group_name TEXT NOT NULL DEFAULT '',
        channel_name TEXT NOT NULL,
        stream_url TEXT NOT NULL,
        logo_url TEXT NOT NULL DEFAULT '',
        sort_order INTEGER NOT NULL
    )
    """
)

entry_columns = {
    row[1]
    for row in conn.execute("PRAGMA table_info(live_source_entry)").fetchall()
}
if "logo_url" not in entry_columns:
    conn.execute("ALTER TABLE live_source_entry ADD COLUMN logo_url TEXT NOT NULL DEFAULT ''")
```

```python
cursor = conn.execute(
    """
    INSERT INTO live_source_entry (source_id, group_name, channel_name, stream_url, logo_url, sort_order)
    VALUES (?, ?, ?, ?, ?, ?)
    """,
    (source_id, group_name, channel_name, stream_url, logo_url, next_order),
)
```

```python
SELECT id, source_id, group_name, channel_name, stream_url, logo_url, sort_order
FROM live_source_entry
```

```python
conn.execute(
    """
    UPDATE live_source_entry
    SET group_name = ?, channel_name = ?, stream_url = ?, logo_url = ?
    WHERE id = ?
    """,
    (group_name, channel_name, stream_url, logo_url, entry_id),
)
```

- [ ] **Step 4: Run the repository tests to verify they pass**

Run: `uv run pytest tests/test_live_source_repository.py -v`
Expected: PASS including the new migration and round-trip tests

- [ ] **Step 5: Commit the storage changes**

```bash
git add src/atv_player/models.py src/atv_player/live_source_repository.py tests/test_live_source_repository.py
git commit -m "feat: store manual live channel logos"
```

## Task 2: Propagate Manual Channel Logos Through The Service Layer

**Files:**
- Modify: `src/atv_player/custom_live_service.py`
- Test: `tests/test_custom_live_service.py`

- [ ] **Step 1: Write the failing service test**

```python
def test_custom_live_service_propagates_manual_entry_logo_to_items_and_request(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    service = CustomLiveService(repo, http_client=FakeHttpClient())
    source = service.add_manual_source("手动源")
    entry = service.add_manual_entry(
        source.id,
        group_name="",
        channel_name="CCTV-1",
        stream_url="https://live.example/cctv1.m3u8",
        logo_url="https://img.example/cctv1.png",
    )

    items, total = service.load_items(f"custom:{source.id}", 1)
    request = service.build_request(f"custom-channel:{source.id}:manual-{entry.id}")

    assert total == 1
    assert items[0].vod_pic == "https://img.example/cctv1.png"
    assert request.vod.vod_pic == "https://img.example/cctv1.png"
```

- [ ] **Step 2: Run the service tests to verify they fail**

Run: `uv run pytest tests/test_custom_live_service.py -v`
Expected: FAIL because manual `ParsedChannel` objects are still created without `logo_url`

- [ ] **Step 3: Update the manual playlist mapping**

```python
channel = ParsedChannel(
    key=f"manual-{entry.id}",
    name=entry.channel_name,
    url=entry.stream_url,
    logo_url=entry.logo_url,
)
```

- [ ] **Step 4: Run the service tests to verify they pass**

Run: `uv run pytest tests/test_custom_live_service.py -v`
Expected: PASS including the new manual-logo propagation test

- [ ] **Step 5: Commit the service mapping**

```bash
git add src/atv_player/custom_live_service.py tests/test_custom_live_service.py
git commit -m "feat: propagate manual live channel logos"
```

## Task 3: Add The Optional Logo Field To The Manual Channel Editor

**Files:**
- Modify: `src/atv_player/ui/manual_live_source_dialog.py`
- Test: `tests/test_live_source_manager_dialog.py`

- [ ] **Step 1: Write the failing dialog tests**

```python
def test_manual_live_source_dialog_adds_entry_with_logo_url(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = ManualLiveSourceDialog(manager, source_id=2)
    qtbot.addWidget(dialog)
    dialog.show()
    monkeypatch.setattr(
        dialog,
        "_prompt_entry",
        lambda **kwargs: ("卫视", "湖南卫视", "https://live.example/hunan.m3u8", "https://img.example/hunan.png"),
    )

    dialog._add_entry()

    assert manager.add_entry_calls == [
        (2, "卫视", "湖南卫视", "https://live.example/hunan.m3u8", "https://img.example/hunan.png")
    ]


def test_manual_live_source_dialog_renders_logo_column(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = ManualLiveSourceDialog(manager, source_id=2)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.reload_entries()

    assert dialog.entry_table.columnCount() == 4
    assert dialog.entry_table.item(0, 3).text() == "https://img.example/cctv1.png"
```

- [ ] **Step 2: Run the dialog tests to verify they fail**

Run: `uv run pytest tests/test_live_source_manager_dialog.py -v`
Expected: FAIL because the form, fake manager, and table still only handle `group_name`, `channel_name`, and `stream_url`

- [ ] **Step 3: Update the fake manager and fixture data in the tests**

```python
LiveSourceEntry(
    id=10,
    source_id=2,
    group_name="央视",
    channel_name="CCTV-1",
    stream_url="https://live.example/cctv1.m3u8",
    logo_url="https://img.example/cctv1.png",
    sort_order=0,
)
```

```python
def add_manual_entry(self, source_id: int, *, group_name: str, channel_name: str, stream_url: str, logo_url: str):
    self.add_entry_calls.append((source_id, group_name, channel_name, stream_url, logo_url))


def update_manual_entry(
    self,
    entry_id: int,
    *,
    group_name: str,
    channel_name: str,
    stream_url: str,
    logo_url: str,
):
    self.update_entry_calls.append((entry_id, group_name, channel_name, stream_url, logo_url))
```

- [ ] **Step 4: Extend the dialog form and table**

```python
self.logo_edit = QLineEdit(logo_url, self)
form.addRow("Logo URL", self.logo_edit)
```

```python
def values(self) -> tuple[str, str, str, str]:
    return (
        self.group_edit.text().strip(),
        self.channel_edit.text().strip(),
        self.url_edit.text().strip(),
        self.logo_edit.text().strip(),
    )
```

```python
self.entry_table = QTableWidget(0, 4, self)
self.entry_table.setHorizontalHeaderLabels(["分组", "频道名", "地址", "Logo"])
header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
```

```python
self.entry_table.setItem(row, 3, QTableWidgetItem(entry.logo_url))
```

```python
group_name, channel_name, stream_url, logo_url = self._prompt_entry(...)
if not channel_name or not stream_url:
    return
self.manager.add_manual_entry(
    self.source_id,
    group_name=group_name,
    channel_name=channel_name,
    stream_url=stream_url,
    logo_url=logo_url,
)
```

```python
self.manager.update_manual_entry(
    entry_id,
    group_name=group_name,
    channel_name=channel_name,
    stream_url=stream_url,
    logo_url=logo_url,
)
```

- [ ] **Step 5: Run the dialog tests to verify they pass**

Run: `uv run pytest tests/test_live_source_manager_dialog.py -v`
Expected: PASS including the new add/edit forwarding and logo-column rendering tests

- [ ] **Step 6: Commit the dialog changes**

```bash
git add src/atv_player/ui/manual_live_source_dialog.py tests/test_live_source_manager_dialog.py
git commit -m "feat: edit manual live channel logos"
```

## Task 4: Run Focused Regression Verification

**Files:**
- Verify only: `tests/test_live_source_repository.py`
- Verify only: `tests/test_custom_live_service.py`
- Verify only: `tests/test_live_source_manager_dialog.py`
- Verify only: `tests/test_live_controller.py`
- Verify only: `tests/test_main_window_ui.py`
- Verify only: `tests/test_app.py`

- [ ] **Step 1: Run the focused regression suite**

Run: `uv run pytest tests/test_live_source_repository.py tests/test_custom_live_service.py tests/test_live_source_manager_dialog.py tests/test_live_controller.py tests/test_main_window_ui.py tests/test_app.py -v`
Expected: PASS with manual-channel logo coverage and no regressions in live browsing, dialog wiring, or app assembly

- [ ] **Step 2: Commit the verified feature state**

```bash
git add src/atv_player/models.py src/atv_player/live_source_repository.py src/atv_player/custom_live_service.py src/atv_player/ui/manual_live_source_dialog.py tests/test_live_source_repository.py tests/test_custom_live_service.py tests/test_live_source_manager_dialog.py
git commit -m "feat: support manual live channel logos"
```

## Self-Review

- Spec coverage:
  Storage migration and persistence are covered by Task 1.
  Existing browse/playback image reuse is covered by Task 2.
  Optional `Logo URL` input and table rendering are covered by Task 3.
  Final regression verification is covered by Task 4.
- Placeholder scan:
  No `TODO`, `TBD`, or unspecified “add tests/handle validation later” placeholders remain.
- Type consistency:
  `logo_url` is used consistently in `LiveSourceEntry`, repository CRUD, service mapping, dialog form values, and tests.
