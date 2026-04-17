# Custom Live Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add managed custom live sources to `网络直播`, including remote `m3u`, local `m3u`, and manual channel sources, with cached browsing and direct playback.

**Architecture:** Keep backend `/live/{token}` behavior unchanged and add a local custom-live stack beside it. Store sources in SQLite, parse `m3u` text into grouped channels, expose custom categories through the existing live controller contract, and manage sources through a new dialog opened from the main window header.

**Tech Stack:** Python 3, SQLite, PySide6, pytest, existing `PosterGridPage` and `OpenPlayerRequest` flow

---

## File Structure

### New Files

- `src/atv_player/live_source_repository.py`
  Local SQLite repository for source definitions, manual channel rows, cache text, and error metadata.
- `src/atv_player/m3u_parser.py`
  Stateless parser that converts `m3u` text into grouped channels.
- `src/atv_player/custom_live_service.py`
  Service that loads custom sources, resolves cache, refreshes remote/local sources, maps groups/channels into `VodItem` and `OpenPlayerRequest`.
- `src/atv_player/ui/live_source_manager_dialog.py`
  Source-level management dialog for add/rename/enable/order/refresh/delete.
- `src/atv_player/ui/manual_live_source_dialog.py`
  Child dialog for manual channel CRUD and reordering.
- `tests/test_live_source_repository.py`
  Repository coverage for schema, default source, CRUD, ordering, cache, and manual entries.
- `tests/test_m3u_parser.py`
  Parser coverage for grouped, ungrouped, and attribute-heavy playlists.
- `tests/test_custom_live_service.py`
  Service coverage for source browsing, refresh, cache fallback, and playback requests.
- `tests/test_live_source_manager_dialog.py`
  UI coverage for source-management actions and manual-channel entry point.

### Modified Files

- `src/atv_player/models.py`
  Add dataclasses for persisted live sources and manual channel entries.
- `src/atv_player/storage.py`
  No schema change for `app_config`, but keep this file untouched unless shared DB helper extraction becomes necessary.
- `src/atv_player/controllers/live_controller.py`
  Merge custom categories ahead of backend categories and route custom ids to the service.
- `src/atv_player/app.py`
  Construct the new repository and service, inject them into `LiveController`, and pass the manager into `MainWindow`.
- `src/atv_player/ui/main_window.py`
  Add the `直播源管理` button, wire dialog opening, and reload live categories after dialog changes.
- `tests/test_live_controller.py`
  Extend controller coverage for custom categories, folder navigation, and custom playback requests.
- `tests/test_app.py`
  Cover button wiring, source manager injection, and player open flow from custom channels.
- `tests/test_main_window_ui.py`
  Cover header button ordering and text.

## Task 1: Add Source Models And Repository

**Files:**
- Create: `src/atv_player/live_source_repository.py`
- Modify: `src/atv_player/models.py`
- Test: `tests/test_live_source_repository.py`

- [ ] **Step 1: Write the failing repository tests**

```python
from pathlib import Path

from atv_player.live_source_repository import LiveSourceRepository


def test_live_source_repository_inserts_default_example_source(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")

    sources = repo.list_sources()

    assert len(sources) == 1
    assert sources[0].display_name == "示例直播源"
    assert sources[0].source_type == "remote"
    assert sources[0].source_value == "https://raw.githubusercontent.com/Rivens7/Livelist/refs/heads/main/IPTV.m3u"
    assert sources[0].enabled is True
    assert sources[0].is_default is True


def test_live_source_repository_round_trips_source_updates_and_manual_entries(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("manual", "", "自建直播")
    repo.update_source(
        source.id,
        display_name="自建频道",
        enabled=False,
        source_value="",
        cache_text="#EXTM3U",
        last_error="解析失败",
        last_refreshed_at=123,
    )
    repo.add_manual_entry(source.id, group_name="央视", channel_name="CCTV-1", stream_url="https://live.example/cctv1.m3u8")

    saved = [item for item in repo.list_sources() if item.id == source.id][0]
    entries = repo.list_manual_entries(source.id)

    assert saved.display_name == "自建频道"
    assert saved.enabled is False
    assert saved.cache_text == "#EXTM3U"
    assert saved.last_error == "解析失败"
    assert saved.last_refreshed_at == 123
    assert [(item.group_name, item.channel_name, item.stream_url) for item in entries] == [
        ("央视", "CCTV-1", "https://live.example/cctv1.m3u8")
    ]


def test_live_source_repository_moves_sources_and_entries_in_sort_order(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    first = repo.add_source("remote", "https://example.com/a.m3u", "A")
    second = repo.add_source("remote", "https://example.com/b.m3u", "B")
    repo.move_source(second.id, -1)
    manual = repo.add_source("manual", "", "手动")
    first_entry = repo.add_manual_entry(manual.id, group_name="", channel_name="一台", stream_url="https://live.example/1.m3u8")
    second_entry = repo.add_manual_entry(manual.id, group_name="", channel_name="二台", stream_url="https://live.example/2.m3u8")
    repo.move_manual_entry(second_entry.id, -1)

    sources = [item.display_name for item in repo.list_sources() if item.display_name in {"A", "B"}]
    entries = [item.channel_name for item in repo.list_manual_entries(manual.id)]

    assert sources == ["B", "A"]
    assert entries == ["二台", "一台"]
```

- [ ] **Step 2: Run the repository tests to verify they fail**

Run: `uv run pytest tests/test_live_source_repository.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'atv_player.live_source_repository'`

- [ ] **Step 3: Add the new dataclasses in `src/atv_player/models.py`**

```python
@dataclass(slots=True)
class LiveSourceConfig:
    id: int = 0
    source_type: str = ""
    source_value: str = ""
    display_name: str = ""
    enabled: bool = True
    sort_order: int = 0
    is_default: bool = False
    last_refreshed_at: int = 0
    last_error: str = ""
    cache_text: str = ""


@dataclass(slots=True)
class LiveSourceEntry:
    id: int = 0
    source_id: int = 0
    group_name: str = ""
    channel_name: str = ""
    stream_url: str = ""
    sort_order: int = 0
```

- [ ] **Step 4: Implement `LiveSourceRepository` in `src/atv_player/live_source_repository.py`**

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

from atv_player.models import LiveSourceConfig, LiveSourceEntry

_DEFAULT_SOURCE_NAME = "示例直播源"
_DEFAULT_SOURCE_URL = "https://raw.githubusercontent.com/Rivens7/Livelist/refs/heads/main/IPTV.m3u"


class LiveSourceRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS live_source (
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
                CREATE TABLE IF NOT EXISTS live_source_entry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '',
                    channel_name TEXT NOT NULL,
                    stream_url TEXT NOT NULL,
                    sort_order INTEGER NOT NULL
                )
                """
            )
            existing = conn.execute("SELECT COUNT(*) FROM live_source WHERE is_default = 1").fetchone()[0]
            if existing == 0:
                next_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM live_source").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO live_source (
                        source_type, source_value, display_name, enabled, sort_order,
                        is_default, last_refreshed_at, last_error, cache_text
                    )
                    VALUES ('remote', ?, ?, 1, ?, 1, 0, '', '')
                    """,
                    (_DEFAULT_SOURCE_URL, _DEFAULT_SOURCE_NAME, next_order),
                )

    def add_source(self, source_type: str, source_value: str, display_name: str) -> LiveSourceConfig:
        with self._connect() as conn:
            next_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM live_source").fetchone()[0]
            cursor = conn.execute(
                """
                INSERT INTO live_source (
                    source_type, source_value, display_name, enabled, sort_order,
                    is_default, last_refreshed_at, last_error, cache_text
                )
                VALUES (?, ?, ?, 1, ?, 0, 0, '', '')
                """,
                (source_type, source_value, display_name, next_order),
            )
        return self.get_source(int(cursor.lastrowid))

    def get_source(self, source_id: int) -> LiveSourceConfig:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, source_type, source_value, display_name, enabled, sort_order,
                       is_default, last_refreshed_at, last_error, cache_text
                FROM live_source
                WHERE id = ?
                """,
                (source_id,),
            ).fetchone()
        assert row is not None
        values = list(row)
        values[4] = bool(values[4])
        values[6] = bool(values[6])
        return LiveSourceConfig(*values)

    def list_sources(self) -> list[LiveSourceConfig]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source_type, source_value, display_name, enabled, sort_order,
                       is_default, last_refreshed_at, last_error, cache_text
                FROM live_source
                ORDER BY sort_order ASC, id ASC
                """
            ).fetchall()
        result: list[LiveSourceConfig] = []
        for row in rows:
            values = list(row)
            values[4] = bool(values[4])
            values[6] = bool(values[6])
            result.append(LiveSourceConfig(*values))
        return result
```

- [ ] **Step 5: Finish the repository write APIs used by the tests**

```python
    def update_source(
        self,
        source_id: int,
        *,
        display_name: str,
        enabled: bool,
        source_value: str,
        cache_text: str,
        last_error: str,
        last_refreshed_at: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE live_source
                SET display_name = ?, enabled = ?, source_value = ?, cache_text = ?,
                    last_error = ?, last_refreshed_at = ?
                WHERE id = ?
                """,
                (display_name, int(enabled), source_value, cache_text, last_error, last_refreshed_at, source_id),
            )

    def move_source(self, source_id: int, direction: int) -> None:
        sources = self.list_sources()
        index = next(i for i, item in enumerate(sources) if item.id == source_id)
        target = index + direction
        if not (0 <= target < len(sources)):
            return
        sources[index], sources[target] = sources[target], sources[index]
        with self._connect() as conn:
            for order, item in enumerate(sources):
                conn.execute("UPDATE live_source SET sort_order = ? WHERE id = ?", (order, item.id))

    def delete_source(self, source_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM live_source_entry WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM live_source WHERE id = ?", (source_id,))

    def add_manual_entry(self, source_id: int, *, group_name: str, channel_name: str, stream_url: str) -> LiveSourceEntry:
        with self._connect() as conn:
            next_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM live_source_entry WHERE source_id = ?",
                (source_id,),
            ).fetchone()[0]
            cursor = conn.execute(
                """
                INSERT INTO live_source_entry (source_id, group_name, channel_name, stream_url, sort_order)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_id, group_name, channel_name, stream_url, next_order),
            )
        return self.get_manual_entry(int(cursor.lastrowid))

    def get_manual_entry(self, entry_id: int) -> LiveSourceEntry:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, source_id, group_name, channel_name, stream_url, sort_order
                FROM live_source_entry
                WHERE id = ?
                """,
                (entry_id,),
            ).fetchone()
        assert row is not None
        return LiveSourceEntry(*row)

    def list_manual_entries(self, source_id: int) -> list[LiveSourceEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source_id, group_name, channel_name, stream_url, sort_order
                FROM live_source_entry
                WHERE source_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (source_id,),
            ).fetchall()
        return [LiveSourceEntry(*row) for row in rows]

    def move_manual_entry(self, entry_id: int, direction: int) -> None:
        entry = self.get_manual_entry(entry_id)
        entries = self.list_manual_entries(entry.source_id)
        index = next(i for i, item in enumerate(entries) if item.id == entry_id)
        target = index + direction
        if not (0 <= target < len(entries)):
            return
        entries[index], entries[target] = entries[target], entries[index]
        with self._connect() as conn:
            for order, item in enumerate(entries):
                conn.execute("UPDATE live_source_entry SET sort_order = ? WHERE id = ?", (order, item.id))
```

- [ ] **Step 6: Run the repository tests to verify they pass**

Run: `uv run pytest tests/test_live_source_repository.py -v`

Expected: PASS for all repository tests

- [ ] **Step 7: Commit the repository layer**

```bash
git add src/atv_player/models.py src/atv_player/live_source_repository.py tests/test_live_source_repository.py
git commit -m "feat: add live source repository"
```

## Task 2: Add M3U Parsing

**Files:**
- Create: `src/atv_player/m3u_parser.py`
- Test: `tests/test_m3u_parser.py`

- [ ] **Step 1: Write the failing parser tests**

```python
from atv_player.m3u_parser import parse_m3u


def test_parse_m3u_groups_channels_by_group_title() -> None:
    playlist = """#EXTM3U
#EXTINF:-1 group-title="央视频道",CCTV-1综合
https://live.example/cctv1.m3u8
#EXTINF:-1 group-title="央视频道",CCTV-2财经
https://live.example/cctv2.m3u8
"""

    parsed = parse_m3u(playlist)

    assert [group.name for group in parsed.groups] == ["央视频道"]
    assert [(item.name, item.url) for item in parsed.groups[0].channels] == [
        ("CCTV-1综合", "https://live.example/cctv1.m3u8"),
        ("CCTV-2财经", "https://live.example/cctv2.m3u8"),
    ]


def test_parse_m3u_keeps_ungrouped_channels_separately() -> None:
    playlist = """#EXTM3U
#EXTINF:-1 tvg-name="CGTN",CGTN英语
https://live.example/cgtn.m3u8
"""

    parsed = parse_m3u(playlist)

    assert parsed.groups == []
    assert [(item.name, item.url) for item in parsed.ungrouped_channels] == [
        ("CGTN英语", "https://live.example/cgtn.m3u8")
    ]


def test_parse_m3u_ignores_comments_and_reads_optional_logo() -> None:
    playlist = """#EXTM3U
#EXTINF:-1 tvg-logo="https://img.example/logo.png" group-title="卫视频道",北京卫视
https://live.example/btv.m3u8
# some comment
"""

    parsed = parse_m3u(playlist)

    assert parsed.groups[0].channels[0].logo_url == "https://img.example/logo.png"
```

- [ ] **Step 2: Run the parser tests to verify they fail**

Run: `uv run pytest tests/test_m3u_parser.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'atv_player.m3u_parser'`

- [ ] **Step 3: Implement the normalized parser dataclasses and parser function**

```python
from __future__ import annotations

from dataclasses import dataclass, field
import re

_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


@dataclass(slots=True)
class ParsedChannel:
    key: str
    name: str
    url: str
    logo_url: str = ""


@dataclass(slots=True)
class ParsedGroup:
    key: str
    name: str
    channels: list[ParsedChannel] = field(default_factory=list)


@dataclass(slots=True)
class ParsedPlaylist:
    groups: list[ParsedGroup] = field(default_factory=list)
    ungrouped_channels: list[ParsedChannel] = field(default_factory=list)


def parse_m3u(text: str) -> ParsedPlaylist:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    result = ParsedPlaylist()
    groups_by_name: dict[str, ParsedGroup] = {}
    pending_name = ""
    pending_group = ""
    pending_logo = ""
    channel_index = 0
    for line in lines:
        if line.startswith("#EXTINF:"):
            attrs = dict(_ATTR_RE.findall(line))
            pending_group = attrs.get("group-title", "").strip()
            pending_logo = attrs.get("tvg-logo", "").strip()
            pending_name = line.rsplit(",", 1)[-1].strip()
            continue
        if line.startswith("#"):
            continue
        if not pending_name:
            continue
        channel = ParsedChannel(
            key=f"channel-{channel_index}",
            name=pending_name,
            url=line,
            logo_url=pending_logo,
        )
        channel_index += 1
        if pending_group:
            group = groups_by_name.get(pending_group)
            if group is None:
                group = ParsedGroup(key=f"group-{len(groups_by_name)}", name=pending_group)
                groups_by_name[pending_group] = group
                result.groups.append(group)
            group.channels.append(channel)
        else:
            result.ungrouped_channels.append(channel)
        pending_name = ""
        pending_group = ""
        pending_logo = ""
    return result
```

- [ ] **Step 4: Run the parser tests to verify they pass**

Run: `uv run pytest tests/test_m3u_parser.py -v`

Expected: PASS for all parser tests

- [ ] **Step 5: Commit the parser**

```bash
git add src/atv_player/m3u_parser.py tests/test_m3u_parser.py
git commit -m "feat: add m3u parser"
```

## Task 3: Add Custom Live Service

**Files:**
- Create: `src/atv_player/custom_live_service.py`
- Modify: `src/atv_player/models.py`
- Test: `tests/test_custom_live_service.py`

- [ ] **Step 1: Write the failing service tests**

```python
from pathlib import Path

from atv_player.custom_live_service import CustomLiveService
from atv_player.live_source_repository import LiveSourceRepository


class FakeHttpClient:
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[str] = []

    def get_text(self, url: str) -> str:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.text


def test_custom_live_service_lists_enabled_sources_as_categories(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    remote = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(remote.id, display_name="自定义远程", enabled=True, source_value=remote.source_value, cache_text="#EXTM3U", last_error="", last_refreshed_at=1)
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    categories = service.load_categories()

    assert [(item.type_id, item.type_name) for item in categories if item.type_name == "自定义远程"] == [
        (f"custom:{remote.id}", "自定义远程")
    ]


def test_custom_live_service_prefers_cache_and_maps_groups_and_channels(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        source.id,
        display_name="自定义远程",
        enabled=True,
        source_value="https://example.com/live.m3u",
        cache_text="#EXTM3U\n#EXTINF:-1 group-title=\"央视频道\",CCTV-1\nhttps://live.example/cctv1.m3u8\n",
        last_error="",
        last_refreshed_at=1,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    items, total = service.load_items(f"custom:{source.id}", 1)

    assert total == 1
    assert [(item.vod_id, item.vod_name, item.vod_tag) for item in items] == [
        (f"custom-folder:{source.id}:group-0", "央视频道", "folder")
    ]


def test_custom_live_service_refresh_failure_preserves_old_cache(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        source.id,
        display_name="自定义远程",
        enabled=True,
        source_value="https://example.com/live.m3u",
        cache_text="#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://live.example/cctv1.m3u8\n",
        last_error="",
        last_refreshed_at=1,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient(error=RuntimeError("timeout")))

    items, total = service.load_items(f"custom:{source.id}", 1)

    assert total == 1
    saved = repo.get_source(source.id)
    assert saved.cache_text.startswith("#EXTM3U")
    assert saved.last_error == ""
```

- [ ] **Step 2: Run the service tests to verify they fail**

Run: `uv run pytest tests/test_custom_live_service.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'atv_player.custom_live_service'`

- [ ] **Step 3: Add the helper dataclass used by the service**

```python
@dataclass(slots=True)
class LiveSourceChannelView:
    source_id: int
    channel_id: str
    group_key: str
    channel_name: str
    stream_url: str
    logo_url: str = ""
```

- [ ] **Step 4: Implement the service category and source-root mapping**

```python
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from atv_player.m3u_parser import ParsedPlaylist, parse_m3u
from atv_player.models import DoubanCategory, OpenPlayerRequest, PlayItem, VodItem


class _HttpTextClient(Protocol):
    def get_text(self, url: str) -> str:
        ...


class CustomLiveService:
    def __init__(self, repository, http_client: _HttpTextClient) -> None:
        self._repository = repository
        self._http_client = http_client

    def load_categories(self) -> list[DoubanCategory]:
        return [
            DoubanCategory(type_id=f"custom:{item.id}", type_name=item.display_name)
            for item in self._repository.list_sources()
            if item.enabled
        ]

    def load_items(self, category_id: str, page: int) -> tuple[list[VodItem], int]:
        source_id = int(category_id.split(":", 1)[1])
        source = self._repository.get_source(source_id)
        playlist = self._load_playlist(source)
        if playlist.groups:
            items = [
                VodItem(
                    vod_id=f"custom-folder:{source.id}:{group.key}",
                    vod_name=group.name,
                    vod_tag="folder",
                )
                for group in playlist.groups
            ]
            return items, len(items)
        items = [
            VodItem(
                vod_id=f"custom-channel:{source.id}:{channel.key}",
                vod_name=channel.name,
                vod_tag="file",
                vod_pic=channel.logo_url,
            )
            for channel in playlist.ungrouped_channels
        ]
        return items, len(items)
```

- [ ] **Step 5: Implement playlist resolution, folder loading, and direct playback**

```python
    def load_folder_items(self, vod_id: str) -> tuple[list[VodItem], int]:
        _prefix, source_id_text, group_key = vod_id.split(":", 2)
        source = self._repository.get_source(int(source_id_text))
        playlist = self._load_playlist(source)
        group = next(item for item in playlist.groups if item.key == group_key)
        items = [
            VodItem(
                vod_id=f"custom-channel:{source.id}:{channel.key}",
                vod_name=channel.name,
                vod_tag="file",
                vod_pic=channel.logo_url,
            )
            for channel in group.channels
        ]
        return items, len(items)

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        _prefix, source_id_text, channel_key = vod_id.split(":", 2)
        source = self._repository.get_source(int(source_id_text))
        playlist = self._load_playlist(source)
        for channel in playlist.ungrouped_channels:
            if channel.key == channel_key:
                return self._build_request_from_channel(source.display_name, channel)
        for group in playlist.groups:
            for channel in group.channels:
                if channel.key == channel_key:
                    return self._build_request_from_channel(channel.name, channel)
        raise ValueError(f"没有可播放的项目: {vod_id}")

    def _build_request_from_channel(self, vod_name: str, channel) -> OpenPlayerRequest:
        return OpenPlayerRequest(
            vod=VodItem(vod_id=channel.key, vod_name=vod_name, vod_pic=channel.logo_url, detail_style="live"),
            playlist=[PlayItem(title=channel.name, url=channel.url, vod_id=channel.key, index=0)],
            clicked_index=0,
            source_kind="live",
            source_mode="custom",
            source_vod_id=channel.key,
            use_local_history=False,
        )

    def _load_playlist(self, source) -> ParsedPlaylist:
        if source.source_type == "manual":
            return self._load_manual_playlist(source.id)
        text = source.cache_text
        if not text:
            try:
                text = self._read_source_text(source)
            except Exception:
                if not source.cache_text:
                    raise
                text = source.cache_text
        return parse_m3u(text)
```

- [ ] **Step 6: Complete the cache-aware read helpers**

```python
    def refresh_source(self, source_id: int) -> None:
        source = self._repository.get_source(source_id)
        if source.source_type == "manual":
            return
        try:
            text = self._read_source_text(source)
        except Exception as exc:
            self._repository.update_source(
                source.id,
                display_name=source.display_name,
                enabled=source.enabled,
                source_value=source.source_value,
                cache_text=source.cache_text,
                last_error=str(exc),
                last_refreshed_at=source.last_refreshed_at,
            )
            raise
        self._repository.update_source(
            source.id,
            display_name=source.display_name,
            enabled=source.enabled,
            source_value=source.source_value,
            cache_text=text,
            last_error="",
            last_refreshed_at=1,
        )

    def _read_source_text(self, source) -> str:
        if source.source_type == "remote":
            return self._http_client.get_text(source.source_value)
        if source.source_type == "local":
            return Path(source.source_value).read_text(encoding="utf-8")
        raise ValueError(f"不支持的直播源类型: {source.source_type}")

    def _load_manual_playlist(self, source_id: int) -> ParsedPlaylist:
        from atv_player.m3u_parser import ParsedChannel, ParsedGroup, ParsedPlaylist

        playlist = ParsedPlaylist()
        groups: dict[str, ParsedGroup] = {}
        for index, entry in enumerate(self._repository.list_manual_entries(source_id)):
            channel = ParsedChannel(key=f"manual-{entry.id}", name=entry.channel_name, url=entry.stream_url)
            if entry.group_name:
                group = groups.get(entry.group_name)
                if group is None:
                    group = ParsedGroup(key=f"group-{len(groups)}", name=entry.group_name)
                    groups[entry.group_name] = group
                    playlist.groups.append(group)
                group.channels.append(channel)
            else:
                playlist.ungrouped_channels.append(channel)
        return playlist
```

- [ ] **Step 7: Run the service tests to verify they pass**

Run: `uv run pytest tests/test_custom_live_service.py -v`

Expected: PASS for all service tests

- [ ] **Step 8: Commit the service layer**

```bash
git add src/atv_player/models.py src/atv_player/custom_live_service.py tests/test_custom_live_service.py
git commit -m "feat: add custom live service"
```

## Task 4: Extend The Live Controller

**Files:**
- Modify: `src/atv_player/controllers/live_controller.py`
- Test: `tests/test_live_controller.py`

- [ ] **Step 1: Extend the failing controller tests for custom categories and playback**

```python
def test_load_categories_prepends_enabled_custom_sources() -> None:
    from atv_player.controllers.live_controller import LiveController

    api = FakeApiClient()
    api.category_payload = {"class": [{"type_id": "bili", "type_name": "哔哩哔哩"}]}

    class FakeCustomService:
        def load_categories(self):
            return [DoubanCategory(type_id="custom:7", type_name="自定义远程")]

    controller = LiveController(api, custom_live_service=FakeCustomService())

    categories = controller.load_categories()

    assert [(item.type_id, item.type_name) for item in categories] == [
        ("custom:7", "自定义远程"),
        ("0", "推荐"),
        ("bili", "哔哩哔哩"),
    ]


def test_load_items_routes_custom_category_ids_to_custom_service() -> None:
    from atv_player.controllers.live_controller import LiveController

    api = FakeApiClient()

    class FakeCustomService:
        def __init__(self) -> None:
            self.calls = []

        def load_categories(self):
            return []

        def load_items(self, category_id: str, page: int):
            self.calls.append((category_id, page))
            return [], 0

    custom = FakeCustomService()
    controller = LiveController(api, custom_live_service=custom)

    controller.load_items("custom:9", 1)

    assert custom.calls == [("custom:9", 1)]
    assert api.item_calls == []


def test_build_request_routes_custom_channel_ids_to_custom_service() -> None:
    from atv_player.controllers.live_controller import LiveController
    from atv_player.models import OpenPlayerRequest, PlayItem, VodItem

    api = FakeApiClient()

    class FakeCustomService:
        def load_categories(self):
            return []

        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="自定义频道"),
                playlist=[PlayItem(title="自定义频道", url="https://live.example/custom.m3u8")],
                clicked_index=0,
                source_kind="live",
                source_mode="custom",
                source_vod_id=vod_id,
                use_local_history=False,
            )

    controller = LiveController(api, custom_live_service=FakeCustomService())

    request = controller.build_request("custom-channel:9:channel-0")

    assert request.source_mode == "custom"
    assert request.use_local_history is False
    assert api.detail_calls == []
```

- [ ] **Step 2: Run the controller tests to verify they fail**

Run: `uv run pytest tests/test_live_controller.py -v`

Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'custom_live_service'`

- [ ] **Step 3: Add custom-service routing to `LiveController`**

```python
class _EmptyCustomLiveService:
    def load_categories(self) -> list[DoubanCategory]:
        return []

    def load_items(self, category_id: str, page: int):
        return [], 0

    def load_folder_items(self, vod_id: str):
        return [], 0

    def build_request(self, vod_id: str):
        raise ValueError(f"没有可播放的项目: {vod_id}")


class LiveController:
    _PAGE_SIZE = 30

    def __init__(self, api_client, custom_live_service=None) -> None:
        self._api_client = api_client
        self._custom_live_service = custom_live_service or _EmptyCustomLiveService()
```

- [ ] **Step 4: Merge custom categories and route custom ids**

```python
    def load_categories(self) -> list[DoubanCategory]:
        payload = self._api_client.list_live_categories()
        categories = [_map_category(item) for item in payload.get("class", [])]
        categories = [category for category in categories if category.type_id != "0"]
        custom_categories = list(self._custom_live_service.load_categories())
        return [*custom_categories, DoubanCategory(type_id="0", type_name="推荐"), *categories]

    def load_items(self, category_id: str, page: int) -> tuple[list[VodItem], int]:
        if category_id.startswith("custom:"):
            return self._custom_live_service.load_items(category_id, page)
        payload = self._api_client.list_live_items(category_id, page=page)
        items = self._map_live_items(payload)
        total_raw = payload.get("total")
        if total_raw is not None:
            total = int(total_raw)
        else:
            pagecount = int(payload.get("pagecount") or 0)
            total = pagecount * self._PAGE_SIZE
        return items, total

    def load_folder_items(self, vod_id: str) -> tuple[list[VodItem], int]:
        if vod_id.startswith("custom-folder:"):
            return self._custom_live_service.load_folder_items(vod_id)
        payload = self._api_client.list_live_items(vod_id, page=1)
        items = self._map_live_items(payload)
        total_raw = payload.get("total")
        total = int(total_raw) if total_raw is not None else len(items)
        return items, total

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        if vod_id.startswith("custom-channel:"):
            return self._custom_live_service.build_request(vod_id)
        payload = self._api_client.get_live_detail(vod_id)
        ...
```

- [ ] **Step 5: Run the controller tests to verify they pass**

Run: `uv run pytest tests/test_live_controller.py -v`

Expected: PASS for existing live tests and the new custom-source tests

- [ ] **Step 6: Commit the controller integration**

```bash
git add src/atv_player/controllers/live_controller.py tests/test_live_controller.py
git commit -m "feat: route custom live sources through live controller"
```

## Task 5: Add The Management Dialogs

**Files:**
- Create: `src/atv_player/ui/live_source_manager_dialog.py`
- Create: `src/atv_player/ui/manual_live_source_dialog.py`
- Test: `tests/test_live_source_manager_dialog.py`

- [ ] **Step 1: Write the failing dialog tests**

```python
from atv_player.models import LiveSourceConfig, LiveSourceEntry
from atv_player.ui.live_source_manager_dialog import LiveSourceManagerDialog


class FakeLiveSourceManager:
    def __init__(self) -> None:
        self.sources = [
            LiveSourceConfig(id=1, source_type="remote", source_value="https://example.com/live.m3u", display_name="远程源", enabled=True, sort_order=0),
            LiveSourceConfig(id=2, source_type="manual", source_value="", display_name="手动源", enabled=True, sort_order=1),
        ]
        self.entries = {
            2: [LiveSourceEntry(id=10, source_id=2, group_name="央视", channel_name="CCTV-1", stream_url="https://live.example/cctv1.m3u8", sort_order=0)]
        }
        self.add_remote_calls = []
        self.add_local_calls = []
        self.add_manual_calls = []
        self.refresh_calls = []

    def list_sources(self):
        return list(self.sources)

    def add_remote_source(self, url: str, display_name: str):
        self.add_remote_calls.append((url, display_name))

    def add_local_source(self, path: str, display_name: str):
        self.add_local_calls.append((path, display_name))

    def add_manual_source(self, display_name: str):
        self.add_manual_calls.append(display_name)

    def refresh_source(self, source_id: int):
        self.refresh_calls.append(source_id)


def test_live_source_manager_dialog_renders_rows_and_actions(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.source_table.selectRow(0)
    monkeypatch.setattr(dialog, "_prompt_remote_source", lambda: ("https://example.com/iptv.m3u", "我的远程源"))

    dialog._add_remote_source()
    dialog._refresh_selected()

    assert dialog.source_table.rowCount() == 2
    assert dialog.source_table.item(0, 0).text() == "远程源"
    assert manager.add_remote_calls == [("https://example.com/iptv.m3u", "我的远程源")]
    assert manager.refresh_calls == [1]


def test_live_source_manager_dialog_shows_manual_editor_button_for_manual_source(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.source_table.selectRow(1)

    dialog._sync_action_state()

    assert dialog.manage_channels_button.isEnabled() is True
```

- [ ] **Step 2: Run the dialog tests to verify they fail**

Run: `uv run pytest tests/test_live_source_manager_dialog.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'atv_player.ui.live_source_manager_dialog'`

- [ ] **Step 3: Implement `LiveSourceManagerDialog`**

```python
from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class LiveSourceManagerDialog(QDialog):
    def __init__(self, manager, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("直播源管理")
        self.resize(920, 520)
        self.source_table = QTableWidget(0, 6, self)
        self.source_table.setHorizontalHeaderLabels(["名称", "类型", "地址", "启用", "状态", "最近刷新"])
        self.source_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.source_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.source_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.add_remote_button = QPushButton("添加远程源")
        self.add_local_button = QPushButton("添加本地源")
        self.add_manual_button = QPushButton("添加手动源")
        self.manage_channels_button = QPushButton("管理频道")
        self.refresh_button = QPushButton("刷新")
        actions = QHBoxLayout()
        for button in (
            self.add_remote_button,
            self.add_local_button,
            self.add_manual_button,
            self.manage_channels_button,
            self.refresh_button,
        ):
            actions.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addLayout(actions)
        layout.addWidget(self.source_table)
        self.add_remote_button.clicked.connect(self._add_remote_source)
        self.add_local_button.clicked.connect(self._add_local_source)
        self.add_manual_button.clicked.connect(self._add_manual_source)
        self.refresh_button.clicked.connect(self._refresh_selected)
        self.source_table.itemSelectionChanged.connect(self._sync_action_state)
        self.reload_sources()
```

- [ ] **Step 4: Implement the data-loading and action methods**

```python
    def reload_sources(self) -> None:
        sources = self.manager.list_sources()
        self.source_table.setRowCount(len(sources))
        for row, source in enumerate(sources):
            self.source_table.setItem(row, 0, QTableWidgetItem(source.display_name))
            self.source_table.setItem(row, 1, QTableWidgetItem(source.source_type))
            self.source_table.setItem(row, 2, QTableWidgetItem(source.source_value))
            self.source_table.setItem(row, 3, QTableWidgetItem("是" if source.enabled else "否"))
            self.source_table.setItem(row, 4, QTableWidgetItem(source.last_error or "正常"))
            self.source_table.setItem(row, 5, QTableWidgetItem(str(source.last_refreshed_at or "")))
            self.source_table.item(row, 0).setData(256, source.id)
            self.source_table.item(row, 0).setData(257, source.source_type)
        self._sync_action_state()

    def _selected_source_id(self) -> int | None:
        row = self.source_table.currentRow()
        if row < 0:
            return None
        item = self.source_table.item(row, 0)
        if item is None:
            return None
        return int(item.data(256))

    def _selected_source_type(self) -> str:
        row = self.source_table.currentRow()
        if row < 0:
            return ""
        item = self.source_table.item(row, 0)
        if item is None:
            return ""
        return str(item.data(257) or "")

    def _sync_action_state(self) -> None:
        source_type = self._selected_source_type()
        self.manage_channels_button.setEnabled(source_type == "manual")

    def _prompt_remote_source(self) -> tuple[str, str]:
        url, accepted = QInputDialog.getText(self, "添加远程源", "M3U URL")
        if not accepted:
            return "", ""
        display_name, accepted = QInputDialog.getText(self, "添加远程源", "显示名称")
        return url.strip(), display_name.strip() if accepted else ""

    def _add_remote_source(self) -> None:
        url, display_name = self._prompt_remote_source()
        if not url or not display_name:
            return
        self.manager.add_remote_source(url, display_name)
        self.reload_sources()

    def _refresh_selected(self) -> None:
        source_id = self._selected_source_id()
        if source_id is None:
            return
        self.manager.refresh_source(source_id)
        self.reload_sources()
```

- [ ] **Step 5: Add the manual-channel dialog shell**

```python
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QTableWidget, QVBoxLayout


class ManualLiveSourceDialog(QDialog):
    def __init__(self, manager, source_id: int, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.source_id = source_id
        self.setWindowTitle("管理频道")
        self.resize(760, 420)
        self.entry_table = QTableWidget(0, 3, self)
        self.entry_table.setHorizontalHeaderLabels(["分组", "频道名", "地址"])
        layout = QVBoxLayout(self)
        layout.addWidget(self.entry_table)
```

- [ ] **Step 6: Run the dialog tests to verify they pass**

Run: `uv run pytest tests/test_live_source_manager_dialog.py -v`

Expected: PASS for the source-manager dialog tests

- [ ] **Step 7: Commit the dialog layer**

```bash
git add src/atv_player/ui/live_source_manager_dialog.py src/atv_player/ui/manual_live_source_dialog.py tests/test_live_source_manager_dialog.py
git commit -m "feat: add live source management dialogs"
```

## Task 6: Wire The App And Main Window

**Files:**
- Modify: `src/atv_player/app.py`
- Modify: `src/atv_player/ui/main_window.py`
- Modify: `tests/test_main_window_ui.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Extend the failing UI and app tests**

```python
def test_main_window_shows_live_source_manager_button_after_plugin_manager(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        live_source_manager=object(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.plugin_manager_button.text() == "插件管理"
    assert window.live_source_manager_button.text() == "直播源管理"


def test_main_window_opens_live_source_manager_dialog_and_reloads_live_categories(qtbot, monkeypatch) -> None:
    controller = FakeLiveController()

    class FakeSourceManager:
        pass

    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=controller,
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        live_source_manager=FakeSourceManager(),
        plugin_manager=FakePluginManager(),
    )
    qtbot.addWidget(window)
    window.show()
    reloaded = []
    monkeypatch.setattr(window.live_page, "reload_categories", lambda: reloaded.append(True))

    class FakeDialog:
        def __init__(self, manager, parent=None) -> None:
            self.manager = manager

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(main_window_module, "LiveSourceManagerDialog", FakeDialog)

    window._open_live_source_manager()

    assert reloaded == [True]
```

- [ ] **Step 2: Run the UI and app tests to verify they fail**

Run: `uv run pytest tests/test_main_window_ui.py tests/test_app.py -v`

Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'live_source_manager'`

- [ ] **Step 3: Wire the repository and service into `AppCoordinator`**

```python
from atv_player.custom_live_service import CustomLiveService
from atv_player.live_source_repository import LiveSourceRepository


class _HttpTextClient:
    def __init__(self, client) -> None:
        self._client = client

    def get_text(self, url: str) -> str:
        response = self._client._client.get(url)
        response.raise_for_status()
        return response.text


class AppCoordinator(QObject):
    def __init__(self, repo: SettingsRepository) -> None:
        super().__init__()
        self.repo = repo
        ...
        if hasattr(repo, "database_path"):
            self._live_source_repository = LiveSourceRepository(repo.database_path)
        else:
            self._live_source_repository = None
```

- [ ] **Step 4: Construct the custom service and pass it into the live controller and main window**

```python
    def _show_main(self):
        self._api_client = self._build_api_client()
        config = self.repo.load_config()
        capabilities = self._load_capabilities(self._api_client)
        spider_plugins = self._plugin_manager.load_enabled_plugins()
        custom_live_service = CustomLiveService(
            self._live_source_repository,
            http_client=_HttpTextClient(self._api_client),
        )
        live_controller = LiveController(self._api_client, custom_live_service=custom_live_service)
        self.main_window = MainWindow(
            ...,
            live_controller=live_controller,
            live_source_manager=custom_live_service,
            ...
        )
```

- [ ] **Step 5: Add the main-window button and dialog open flow**

```python
from atv_player.ui.live_source_manager_dialog import LiveSourceManagerDialog


class MainWindow(QMainWindow):
    def __init__(
        self,
        ...,
        live_source_manager=None,
        plugin_manager=None,
        ...
    ) -> None:
        super().__init__()
        self._live_source_manager = live_source_manager
        self.plugin_manager_button = QPushButton("插件管理")
        self.live_source_manager_button = QPushButton("直播源管理")
        self.logout_button = QPushButton("退出登录")
        ...
        self.plugin_manager_button.clicked.connect(self._open_plugin_manager)
        self.live_source_manager_button.clicked.connect(self._open_live_source_manager)
        header_layout = QHBoxLayout()
        header_layout.addStretch(1)
        header_layout.addWidget(self.plugin_manager_button)
        header_layout.addWidget(self.live_source_manager_button)
        header_layout.addWidget(self.logout_button)

    def _open_live_source_manager(self) -> None:
        if self._live_source_manager is None:
            return
        dialog = LiveSourceManagerDialog(self._live_source_manager, self)
        dialog.exec()
        self.live_page.reload_categories()
```

- [ ] **Step 6: Run the focused UI and app tests to verify they pass**

Run: `uv run pytest tests/test_main_window_ui.py tests/test_app.py -v`

Expected: PASS for the new button-order and dialog-wiring tests, plus existing unaffected app tests

- [ ] **Step 7: Commit the app wiring**

```bash
git add src/atv_player/app.py src/atv_player/ui/main_window.py tests/test_main_window_ui.py tests/test_app.py
git commit -m "feat: wire custom live source management"
```

## Task 7: Broaden Verification And Finish Manual-Channel Integration

**Files:**
- Modify: `src/atv_player/ui/live_source_manager_dialog.py`
- Modify: `src/atv_player/ui/manual_live_source_dialog.py`
- Modify: `tests/test_live_source_manager_dialog.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Add one final failing test for manual-channel management**

```python
def test_manual_live_source_dialog_renders_existing_channels(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = ManualLiveSourceDialog(manager, source_id=2)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.reload_entries()

    assert dialog.entry_table.rowCount() == 1
    assert dialog.entry_table.item(0, 0).text() == "央视"
    assert dialog.entry_table.item(0, 1).text() == "CCTV-1"
```

- [ ] **Step 2: Run the dialog test to verify it fails**

Run: `uv run pytest tests/test_live_source_manager_dialog.py::test_manual_live_source_dialog_renders_existing_channels -v`

Expected: FAIL with `AttributeError: 'ManualLiveSourceDialog' object has no attribute 'reload_entries'`

- [ ] **Step 3: Finish the manual dialog data loading**

```python
class ManualLiveSourceDialog(QDialog):
    ...
    def reload_entries(self) -> None:
        entries = self.manager.list_manual_entries(self.source_id)
        self.entry_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self.entry_table.setItem(row, 0, QTableWidgetItem(entry.group_name))
            self.entry_table.setItem(row, 1, QTableWidgetItem(entry.channel_name))
            self.entry_table.setItem(row, 2, QTableWidgetItem(entry.stream_url))
            self.entry_table.item(row, 0).setData(256, entry.id)
```

- [ ] **Step 4: Run the focused dialogs, repository, service, controller, and app suites**

Run: `uv run pytest tests/test_live_source_repository.py tests/test_m3u_parser.py tests/test_custom_live_service.py tests/test_live_controller.py tests/test_live_source_manager_dialog.py tests/test_main_window_ui.py tests/test_app.py -v`

Expected: PASS across the custom-live feature suites

- [ ] **Step 5: Run the broader regression suites that touch existing storage and live browsing**

Run: `uv run pytest tests/test_storage.py tests/test_poster_grid_page_ui.py tests/test_live_controller.py tests/test_app.py -v`

Expected: PASS with no regressions in existing storage, poster-grid, live, or app flows

- [ ] **Step 6: Commit the completed feature**

```bash
git add src/atv_player/ui/live_source_manager_dialog.py src/atv_player/ui/manual_live_source_dialog.py tests/test_live_source_manager_dialog.py
git commit -m "feat: complete custom live source management"
```

## Self-Review

- Spec coverage check:
  All source types are covered by Tasks 1, 3, 5, and 7.
  Header button and dialog entry are covered by Task 6.
  Cached-first remote browsing and refresh fallback are covered by Task 3.
  Custom categories, folders, and playback are covered by Task 4.
  Example source initialization is covered by Task 1.
- Placeholder scan:
  No `TODO`, `TBD`, or deferred placeholders remain in the task steps.
- Type consistency:
  The plan uses `LiveSourceConfig`, `LiveSourceEntry`, `parse_m3u`, `CustomLiveService`, `LiveSourceManagerDialog`, and `ManualLiveSourceDialog` consistently across later tasks.
