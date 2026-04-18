# Custom Live EPG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a global custom-live `EPG URL` with cached XMLTV refresh, support for plain XMLTV and `.xml.gz` downloads, startup background updates, and immediate `当前节目` / `下一节目` display in the player metadata panel for custom live playback.

**Architecture:** Keep global EPG persistence separate from `live_source` by adding a dedicated repository and service. `LiveEpgService` owns XMLTV refresh, gzip decompression, parsing, and channel lookup; `CustomLiveService` consumes it only when building custom live playback requests; `AppCoordinator` triggers non-blocking startup refresh for both EPG and remote custom sources. The player metadata renderer stays on the existing `detail_style="live"` path and appends two optional EPG rows from `VodItem`.

**Tech Stack:** Python 3, sqlite3, pytest, PySide6, existing `CustomLiveService`, `AppCoordinator`, and player metadata UI

---

## File Structure

### Created Files

- `src/atv_player/live_epg_repository.py`
  Persist the single global EPG config row and its cached XMLTV refresh state.
- `src/atv_player/live_epg_service.py`
  Download XMLTV bytes, decompress gzip payloads when needed, parse XMLTV, normalize channel names, and resolve current/next programme summaries.
- `tests/test_live_epg_repository.py`
  Repository coverage for table creation, round-trip config updates, refresh persistence, and migration.
- `tests/test_live_epg_service.py`
  Service coverage for XMLTV parsing, channel matching, schedule lookup, and refresh failure cache preservation.

### Modified Files

- `src/atv_player/models.py`
  Add `LiveEpgConfig` plus `VodItem.epg_current` / `VodItem.epg_next`.
- `src/atv_player/custom_live_service.py`
  Inject the EPG service and enrich custom live playback requests with current/next programme strings.
- `src/atv_player/ui/player_window.py`
  Append EPG rows for live metadata when the `VodItem` carries them.
- `src/atv_player/ui/live_source_manager_dialog.py`
  Add the global EPG controls above the source table and wire save/manual refresh actions.
- `src/atv_player/app.py`
  Construct the EPG repository/service and start startup background refresh threads for EPG and remote custom sources.
- `src/atv_player/api.py`
  Add a raw-byte download helper for EPG refresh so `.xml.gz` payloads can be decompressed before decoding.
- `tests/test_custom_live_service.py`
  Verify custom live playback requests include EPG text when channels match.
- `tests/test_player_window_ui.py`
  Verify the player metadata panel renders `当前节目` / `下一节目` for custom live sessions and stays unchanged otherwise.
- `tests/test_live_source_manager_dialog.py`
  Verify the dialog renders and wires the global EPG controls.
- `tests/test_app.py`
  Verify `AppCoordinator` starts non-blocking startup refresh for configured EPG and remote custom sources.
- `tests/test_api_client.py`
  Verify the new raw-byte helper returns response bytes for `.xml.gz` downloads.

### Existing Files To Read Before Editing

- `src/atv_player/live_source_repository.py`
  Follow its sqlite migration and row-mapping style for the new repository.
- `src/atv_player/custom_live_service.py`
  Reuse the current custom live request-building flow.
- `src/atv_player/ui/live_source_manager_dialog.py`
  Follow its button wiring and row reload conventions.
- `src/atv_player/ui/player_window.py`
  Extend the current live metadata formatting path instead of introducing a new details surface.
- `src/atv_player/app.py`
  Follow existing startup wiring and background-thread patterns.

## Task 1: Add Global EPG Persistence And Model Fields

**Files:**
- Modify: `src/atv_player/models.py`
- Create: `src/atv_player/live_epg_repository.py`
- Create: `tests/test_live_epg_repository.py`
- Test: `tests/test_live_epg_repository.py`

- [ ] **Step 1: Write the failing repository and model tests**

```python
from pathlib import Path

from atv_player.live_epg_repository import LiveEpgRepository


def test_live_epg_repository_creates_default_config_row(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")

    config = repo.load()

    assert config.id == 1
    assert config.epg_url == ""
    assert config.cache_text == ""
    assert config.last_refreshed_at == 0
    assert config.last_error == ""


def test_live_epg_repository_round_trips_url_without_clearing_cache(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_refresh_result(cache_text="<tv />", last_refreshed_at=9, last_error="")

    repo.save_url("https://example.com/epg.xml")

    config = repo.load()
    assert config.epg_url == "https://example.com/epg.xml"
    assert config.cache_text == "<tv />"
    assert config.last_refreshed_at == 9
```

- [ ] **Step 2: Run the focused repository tests to verify they fail**

Run: `uv run pytest tests/test_live_epg_repository.py -v`

Expected: FAIL with `ModuleNotFoundError` because `atv_player.live_epg_repository` does not exist yet

- [ ] **Step 3: Add the EPG config dataclass to the shared models**

```python
@dataclass(slots=True)
class LiveEpgConfig:
    id: int = 1
    epg_url: str = ""
    cache_text: str = ""
    last_refreshed_at: int = 0
    last_error: str = ""
```

```python
@dataclass(slots=True)
class VodItem:
    vod_id: str
    vod_name: str
    detail_style: str = ""
    path: str = ""
    share_type: str = ""
    vod_pic: str = ""
    vod_tag: str = ""
    vod_time: str = ""
    vod_remarks: str = ""
    vod_play_from: str = ""
    vod_play_url: str = ""
    type_name: str = ""
    vod_content: str = ""
    vod_year: str = ""
    vod_area: str = ""
    vod_lang: str = ""
    vod_director: str = ""
    vod_actor: str = ""
    epg_current: str = ""
    epg_next: str = ""
    dbid: int = 0
    type: int = 0
    items: list[PlayItem] = field(default_factory=list)
```

- [ ] **Step 4: Implement the dedicated EPG repository**

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

from atv_player.models import LiveEpgConfig


class LiveEpgRepository:
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
                CREATE TABLE IF NOT EXISTS live_epg_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    epg_url TEXT NOT NULL DEFAULT '',
                    cache_text TEXT NOT NULL DEFAULT '',
                    last_refreshed_at INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                INSERT INTO live_epg_config (id, epg_url, cache_text, last_refreshed_at, last_error)
                VALUES (1, '', '', 0, '')
                ON CONFLICT(id) DO NOTHING
                """
            )

    def load(self) -> LiveEpgConfig:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, epg_url, cache_text, last_refreshed_at, last_error
                FROM live_epg_config
                WHERE id = 1
                """
            ).fetchone()
        assert row is not None
        return LiveEpgConfig(*row)

    def save_url(self, epg_url: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE live_epg_config SET epg_url = ? WHERE id = 1", (epg_url,))

    def save_refresh_result(self, *, cache_text: str, last_refreshed_at: int, last_error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE live_epg_config
                SET cache_text = ?, last_refreshed_at = ?, last_error = ?
                WHERE id = 1
                """,
                (cache_text, last_refreshed_at, last_error),
            )
```

- [ ] **Step 5: Extend the repository tests for refresh persistence and migration, then run them green**

```python
import sqlite3


def test_live_epg_repository_persists_refresh_result(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")

    repo.save_refresh_result(cache_text="<tv>cached</tv>", last_refreshed_at=17, last_error="broken")

    config = repo.load()
    assert config.cache_text == "<tv>cached</tv>"
    assert config.last_refreshed_at == 17
    assert config.last_error == "broken"


def test_live_epg_repository_creates_table_for_existing_database(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE app_config (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO app_config (id) VALUES (1)")

    repo = LiveEpgRepository(db_path)

    assert repo.load().id == 1
```

Run: `uv run pytest tests/test_live_epg_repository.py -v`

Expected: PASS for creation, round-trip, refresh persistence, and migration coverage

- [ ] **Step 6: Commit the persistence layer**

```bash
git add src/atv_player/models.py src/atv_player/live_epg_repository.py tests/test_live_epg_repository.py
git commit -m "feat: add live epg repository"
```

## Task 2: Add XMLTV Refresh, Parsing, And Channel Matching

**Files:**
- Create: `src/atv_player/live_epg_service.py`
- Create: `tests/test_live_epg_service.py`
- Modify: `src/atv_player/api.py`
- Modify: `tests/test_api_client.py`
- Test: `tests/test_live_epg_service.py`
- Test: `tests/test_api_client.py`

- [ ] **Step 1: Write the failing XMLTV parsing and lookup tests**

```python
from pathlib import Path

from atv_player.live_epg_repository import LiveEpgRepository
from atv_player.live_epg_service import LiveEpgService


class FakeHttpBytesClient:
    def __init__(self, payload: bytes = b"", exc: Exception | None = None) -> None:
        self.payload = payload
        self.exc = exc
        self.calls: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if self.exc is not None:
            raise self.exc
        return self.payload


def test_live_epg_service_returns_current_and_next_programme_from_cached_xmltv(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_refresh_result(
        cache_text=(
            "<tv>"
            '<channel id="c1"><display-name>CCTV-1</display-name></channel>'
            '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>朝闻天下</title></programme>'
            '<programme start="20260418100000 +0800" stop="20260418110000 +0800" channel="c1"><title>新闻30分</title></programme>'
            "</tv>"
        ),
        last_refreshed_at=1,
        last_error="",
    )
    service = LiveEpgService(repo, FakeHttpBytesClient())

    schedule = service.get_schedule("CCTV-1", now_text="2026-04-18T09:30:00+08:00")

    assert schedule is not None
    assert schedule.current == "09:00-10:00 朝闻天下"
    assert schedule.next == "10:00-11:00 新闻30分"


def test_live_epg_service_matches_cctv_names_after_normalization(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_refresh_result(
        cache_text=(
            "<tv>"
            '<channel id="c1"><display-name>CCTV1综合</display-name></channel>'
            '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>朝闻天下</title></programme>'
            "</tv>"
        ),
        last_refreshed_at=1,
        last_error="",
    )
    service = LiveEpgService(repo, FakeHttpBytesClient())

    schedule = service.get_schedule("CCTV-1综合", now_text="2026-04-18T09:30:00+08:00")

    assert schedule is not None
    assert schedule.current == "09:00-10:00 朝闻天下"
    assert schedule.next == ""
```

- [ ] **Step 2: Run the focused service tests to verify they fail**

Run: `uv run pytest tests/test_live_epg_service.py tests/test_api_client.py::test_api_client_get_bytes_returns_raw_content -v`

Expected: FAIL with `ModuleNotFoundError` because `atv_player.live_epg_service` does not exist yet

- [ ] **Step 3: Implement the XMLTV parser, normalization rules, and schedule lookup**

```python
from __future__ import annotations

import gzip
from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree


@dataclass(slots=True)
class EpgSchedule:
    current: str = ""
    next: str = ""


class LiveEpgService:
    def __init__(self, repository, http_client) -> None:
        self._repository = repository
        self._http_client = http_client

    def load_config(self):
        return self._repository.load()

    def save_url(self, epg_url: str) -> None:
        self._repository.save_url(epg_url)

    def get_schedule(self, channel_name: str, *, now_text: str | None = None) -> EpgSchedule | None:
        config = self._repository.load()
        if not config.cache_text.strip():
            return None
        now = datetime.fromisoformat(now_text) if now_text else datetime.now().astimezone()
        channel_names_by_id, programmes = self._parse_xmltv(config.cache_text)
        channel_id = self._match_channel_id(channel_name, channel_names_by_id)
        if not channel_id:
            return None
        current = None
        following = None
        for item in programmes:
            if item["channel"] != channel_id:
                continue
            if item["start"] <= now < item["stop"]:
                current = item
                continue
            if current is not None and item["start"] >= current["stop"]:
                following = item
                break
        if current is None:
            return None
        return EpgSchedule(
            current=self._format_programme(current),
            next=self._format_programme(following) if following is not None else "",
        )

    def _load_xmltv_text(self, url: str) -> str:
        payload = self._http_client.get_bytes(url)
        if payload[:2] == b"\x1f\x8b":
            payload = gzip.decompress(payload)
        return payload.decode("utf-8")
```

```python
    def _normalize_name(self, value: str) -> str:
        normalized = (
            value.strip()
            .lower()
            .replace(" ", "")
            .replace("-", "")
            .replace("_", "")
            .replace("（", "(")
            .replace("）", ")")
        )
        return normalized
```

- [ ] **Step 4: Add refresh behavior with cache preservation and verify the full service suite passes**

```python
    def refresh(self) -> None:
        config = self._repository.load()
        if not config.epg_url.strip():
            return
        try:
            text = self._load_xmltv_text(config.epg_url)
            self._parse_xmltv(text)
        except Exception as exc:
            self._repository.save_refresh_result(
                cache_text=config.cache_text,
                last_refreshed_at=config.last_refreshed_at,
                last_error=str(exc),
            )
            raise
        self._repository.save_refresh_result(
            cache_text=text,
            last_refreshed_at=max(1, config.last_refreshed_at + 1),
            last_error="",
        )
```

```python
def test_live_epg_service_refresh_preserves_old_cache_on_failure(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_url("https://example.com/epg.xml")
    repo.save_refresh_result(cache_text="<tv>old</tv>", last_refreshed_at=3, last_error="")
    service = LiveEpgService(repo, FakeHttpBytesClient(exc=RuntimeError("boom")))

    try:
        service.refresh()
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("expected refresh to fail")

    config = repo.load()
    assert config.cache_text == "<tv>old</tv>"
    assert config.last_refreshed_at == 3
    assert config.last_error == "boom"


def test_live_epg_service_decompresses_gzip_xmltv_payload(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_url("https://example.com/e9.xml.gz")
    payload = gzip.compress(
        (
            "<tv>"
            '<channel id="c1"><display-name>CCTV-1</display-name></channel>'
            '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>朝闻天下</title></programme>'
            "</tv>"
        ).encode("utf-8")
    )
    service = LiveEpgService(repo, FakeHttpBytesClient(payload=payload))

    service.refresh()

    assert "CCTV-1" in repo.load().cache_text
```

```python
def test_api_client_get_bytes_returns_raw_content() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"\x1f\x8bcompressed")
    )
    client = ApiClient("http://127.0.0.1:4567", transport=transport)

    assert client.get_bytes("https://example.com/e9.xml.gz") == b"\x1f\x8bcompressed"
```

```python
def get_bytes(self, url: str) -> bytes:
    try:
        response = self._client.get(url, follow_redirects=True)
        response.raise_for_status()
    except httpx.ReadTimeout as exc:
        raise ApiError("请求超时") from exc
    except httpx.TimeoutException as exc:
        raise ApiError("请求超时") from exc
    except httpx.HTTPError as exc:
        raise ApiError("网络请求失败") from exc
    return response.content
```

Run: `uv run pytest tests/test_live_epg_service.py tests/test_api_client.py::test_api_client_get_bytes_returns_raw_content -v`

Expected: PASS for parsing, normalization, gzip decompression, raw-byte fetching, schedule lookup, unmatched channels, and refresh failure behavior

- [ ] **Step 5: Commit the EPG service**

```bash
git add src/atv_player/live_epg_service.py src/atv_player/api.py tests/test_live_epg_service.py tests/test_api_client.py
git commit -m "feat: add live epg service"
```

## Task 3: Enrich Custom Live Playback Requests And Player Metadata

**Files:**
- Modify: `src/atv_player/custom_live_service.py`
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_custom_live_service.py`
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_custom_live_service.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing custom live request and player metadata tests**

```python
def test_custom_live_service_build_request_adds_epg_summary_for_matching_channel(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("manual", "", "手动源")
    repo.add_manual_entry(
        source.id,
        group_name="",
        channel_name="CCTV-1",
        stream_url="https://live.example/cctv1.m3u8",
    )

    class FakeEpgService:
        def get_schedule(self, channel_name: str):
            assert channel_name == "CCTV-1"
            return type("Schedule", (), {"current": "09:00-10:00 朝闻天下", "next": "10:00-11:00 新闻30分"})()

    service = CustomLiveService(repo, http_client=FakeHttpClient(""), epg_service=FakeEpgService())

    request = service.build_request(f"custom-channel:{source.id}:manual-1")

    assert request.vod.epg_current == "09:00-10:00 朝闻天下"
    assert request.vod.epg_next == "10:00-11:00 新闻30分"
```

```python
def test_player_window_renders_epg_rows_for_live_metadata(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(
            vod_id="custom-live-1",
            vod_name="CCTV-1",
            type_name="直播",
            vod_director="自定义直播",
            detail_style="live",
            epg_current="09:00-10:00 朝闻天下",
            epg_next="10:00-11:00 新闻30分",
        ),
        playlist=[PlayItem(title="线路 1", url="https://live.example/cctv1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    assert "当前节目: 09:00-10:00 朝闻天下" in window.metadata_view.toPlainText()
    assert "下一节目: 10:00-11:00 新闻30分" in window.metadata_view.toPlainText()
```

- [ ] **Step 2: Run the focused playback tests to verify they fail**

Run: `uv run pytest tests/test_custom_live_service.py::test_custom_live_service_build_request_adds_epg_summary_for_matching_channel tests/test_player_window_ui.py::test_player_window_renders_epg_rows_for_live_metadata -v`

Expected: FAIL because `CustomLiveService` does not yet accept an EPG service and the player does not yet render the new rows

- [ ] **Step 3: Inject the EPG service into custom live playback request building**

```python
class CustomLiveService:
    def __init__(self, repository, http_client: _HttpTextClient, epg_service=None) -> None:
        self._repository = repository
        self._http_client = http_client
        self._epg_service = epg_service

    def load_epg_config(self):
        if self._epg_service is None:
            raise RuntimeError("缺少 EPG 服务")
        return self._epg_service.load_config()

    def save_epg_url(self, epg_url: str) -> None:
        if self._epg_service is None:
            raise RuntimeError("缺少 EPG 服务")
        self._epg_service.save_url(epg_url)

    def refresh_epg(self) -> None:
        if self._epg_service is None:
            raise RuntimeError("缺少 EPG 服务")
        self._epg_service.refresh()
```

```python
    def _build_request_from_channel(self, view: _MergedChannelView) -> OpenPlayerRequest:
        epg_current = ""
        epg_next = ""
        if self._epg_service is not None:
            schedule = self._epg_service.get_schedule(view.channel_name)
            if schedule is not None:
                epg_current = schedule.current
                epg_next = schedule.next
        return OpenPlayerRequest(
            vod=VodItem(
                vod_id=view.channel_id,
                vod_name=view.channel_name,
                vod_pic=self._resolve_channel_poster(view),
                detail_style="live",
                epg_current=epg_current,
                epg_next=epg_next,
            ),
            playlist=[
                PlayItem(
                    title=f"{view.channel_name} {index + 1}" if len(view.lines) > 1 else view.channel_name,
                    url=line.url,
                    vod_id=view.channel_id,
                    index=index,
                    headers=dict(line.headers),
                )
                for index, line in enumerate(view.lines)
            ],
            clicked_index=0,
            source_kind="live",
            source_mode="custom",
            source_vod_id=view.channel_id,
            use_local_history=False,
        )
```

- [ ] **Step 4: Extend the live metadata formatter and run the playback tests green**

```python
    def _format_metadata_text(self, vod) -> str:
        if getattr(vod, "detail_style", "") == "live":
            rows = [
                ("标题", vod.vod_name),
                ("平台", vod.vod_director),
                ("类型", vod.type_name),
                ("主播", vod.vod_actor),
                ("人气", vod.vod_remarks),
            ]
            if getattr(vod, "epg_current", ""):
                rows.append(("当前节目", vod.epg_current))
            if getattr(vod, "epg_next", ""):
                rows.append(("下一节目", vod.epg_next))
            return "\n".join(f"{label}: {value}".rstrip() for label, value in rows)
```

Run: `uv run pytest tests/test_custom_live_service.py tests/test_player_window_ui.py -v`

Expected: PASS for the new custom-live EPG coverage and the existing live metadata tests

- [ ] **Step 5: Commit the playback integration**

```bash
git add src/atv_player/custom_live_service.py src/atv_player/ui/player_window.py tests/test_custom_live_service.py tests/test_player_window_ui.py
git commit -m "feat: show epg in custom live player details"
```

## Task 4: Add Global EPG Controls To Live Source Manager

**Files:**
- Modify: `src/atv_player/ui/live_source_manager_dialog.py`
- Modify: `tests/test_live_source_manager_dialog.py`
- Test: `tests/test_live_source_manager_dialog.py`

- [ ] **Step 1: Write the failing dialog tests for EPG controls**

```python
def test_live_source_manager_dialog_renders_global_epg_controls(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)

    assert dialog.epg_url_edit.text() == "https://example.com/epg.xml"
    assert dialog.save_epg_button.text() == "保存"
    assert dialog.refresh_epg_button.text() == "立即更新"


def test_live_source_manager_dialog_saves_global_epg_url(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.epg_url_edit.setText("https://live.example/epg.xml")

    dialog._save_epg_url()

    assert manager.save_epg_url_calls == ["https://live.example/epg.xml"]


def test_live_source_manager_dialog_refreshes_epg_in_background(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    refreshed = threading.Event()

    def fake_refresh() -> None:
        manager.refresh_epg_calls.append(None)
        refreshed.set()

    monkeypatch.setattr(manager, "refresh_epg", fake_refresh)

    dialog._refresh_epg()

    assert refreshed.wait(timeout=1)
```

- [ ] **Step 2: Run the focused dialog tests to verify they fail**

Run: `uv run pytest tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_renders_global_epg_controls tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_saves_global_epg_url -v`

Expected: FAIL because the dialog and fake manager do not yet expose any EPG controls

- [ ] **Step 3: Extend the fake manager and dialog with the global EPG section**

```python
class FakeLiveSourceManager:
    def __init__(self) -> None:
        self.epg_config = type(
            "Config",
            (),
            {
                "epg_url": "https://example.com/epg.xml",
                "last_error": "",
                "last_refreshed_at": 12,
            },
        )()
        self.save_epg_url_calls: list[str] = []
        self.refresh_epg_calls: list[None] = []

    def load_epg_config(self):
        return self.epg_config

    def save_epg_url(self, url: str):
        self.save_epg_url_calls.append(url)

    def refresh_epg(self):
        self.refresh_epg_calls.append(None)
```

```python
        self.epg_url_edit = QLineEdit()
        self.save_epg_button = QPushButton("保存")
        self.refresh_epg_button = QPushButton("立即更新")
        self.epg_status_label = QLabel("")
        epg_row = QHBoxLayout()
        epg_row.addWidget(QLabel("EPG URL"))
        epg_row.addWidget(self.epg_url_edit, 1)
        epg_row.addWidget(self.save_epg_button)
        epg_row.addWidget(self.refresh_epg_button)
        layout.addLayout(epg_row)
        layout.addWidget(self.epg_status_label)
```

- [ ] **Step 4: Wire save/manual refresh behavior on a background thread and run the full dialog suite**

```python
    def _load_epg_config(self) -> None:
        config = self.manager.load_epg_config()
        self.epg_url_edit.setText(config.epg_url)
        self.epg_status_label.setText(config.last_error or str(config.last_refreshed_at or ""))

    def _save_epg_url(self) -> None:
        self.manager.save_epg_url(self.epg_url_edit.text().strip())
        self._load_epg_config()

    def _refresh_epg(self) -> None:
        self.epg_status_label.setText("更新中...")

        def run() -> None:
            try:
                self.manager.refresh_epg()
            finally:
                QTimer.singleShot(0, self._load_epg_config)

        threading.Thread(target=run, daemon=True).start()
```

Run: `uv run pytest tests/test_live_source_manager_dialog.py -v`

Expected: PASS for the new EPG control coverage and the existing source-management dialog behavior

- [ ] **Step 5: Commit the dialog wiring**

```bash
git add src/atv_player/ui/live_source_manager_dialog.py tests/test_live_source_manager_dialog.py
git commit -m "feat: add global epg controls to live source manager"
```

## Task 5: Start Background Refresh From App Startup

**Files:**
- Modify: `src/atv_player/app.py`
- Modify: `tests/test_app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing startup refresh test**

```python
def test_app_coordinator_starts_epg_and_remote_live_refresh_in_background(monkeypatch, qtbot) -> None:
    class FakeRepo:
        database_path = Path("/tmp/app.db")

        def load_config(self):
            return AppConfig(base_url="http://127.0.0.1:4567", token="token", vod_token="vod")

        def save_config(self, config):
            return None

        def clear_token(self):
            return None

    class FakeEpgService:
        def __init__(self) -> None:
            self.refresh_calls = 0
            self._event = threading.Event()

        def load_config(self):
            return type("Config", (), {"epg_url": "https://example.com/epg.xml"})()

        def refresh(self):
            self.refresh_calls += 1
            self._event.set()

        def save_url(self, epg_url: str):
            return None

    class FakeLiveSourceManager:
        def list_sources(self):
            return [type("Source", (), {"id": 1, "source_type": "remote"})()]

        def refresh_source(self, source_id: int):
            assert source_id == 1
            refresh_event.set()

    refresh_event = threading.Event()
    epg_service = FakeEpgService()
```

```python
    coordinator = AppCoordinator(FakeRepo())
    coordinator._live_source_repository = type("Repo", (), {})()
    coordinator._live_epg_repository = type("Repo", (), {})()
    coordinator._plugin_manager = FakePluginManager()
    monkeypatch.setattr(app_module, "LiveEpgRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "LiveEpgService", lambda repository, http_client: epg_service)
    monkeypatch.setattr(app_module, "CustomLiveService", lambda repository, http_client, epg_service=None: FakeLiveSourceManager())
    monkeypatch.setattr(app_module, "ApiClient", FakeApiClient)

    window = coordinator._show_main()

    assert isinstance(window, MainWindow)
    assert epg_service._event.wait(timeout=1)
    assert refresh_event.wait(timeout=1)
```

- [ ] **Step 2: Run the focused startup test to verify it fails**

Run: `uv run pytest tests/test_app.py::test_app_coordinator_starts_epg_and_remote_live_refresh_in_background -v`

Expected: FAIL because `AppCoordinator` does not yet construct the EPG service or trigger startup refresh

- [ ] **Step 3: Wire the repository and service into app startup**

```python
from atv_player.live_epg_repository import LiveEpgRepository
from atv_player.live_epg_service import LiveEpgService
```

```python
        if hasattr(repo, "database_path"):
            self._live_source_repository = LiveSourceRepository(repo.database_path)
            self._live_epg_repository = LiveEpgRepository(repo.database_path)
```

```python
        live_epg_service = LiveEpgService(
            self._live_epg_repository,
            http_client=_HttpTextClient(self._api_client),
        )
        live_source_manager = CustomLiveService(
            self._live_source_repository,
            http_client=_HttpTextClient(self._api_client),
            epg_service=live_epg_service,
        )
```

- [ ] **Step 4: Add non-blocking startup refresh and run the app suite green**

```python
    def _start_live_background_refresh(self, live_source_manager, live_epg_service) -> None:
        def refresh_epg() -> None:
            try:
                if live_epg_service.load_config().epg_url.strip():
                    live_epg_service.refresh()
            except Exception:
                return

        def refresh_sources() -> None:
            for source in live_source_manager.list_sources():
                if source.source_type != "remote":
                    continue
                try:
                    live_source_manager.refresh_source(source.id)
                except Exception:
                    continue

        threading.Thread(target=refresh_epg, daemon=True).start()
        threading.Thread(target=refresh_sources, daemon=True).start()
```

```python
        self._start_live_background_refresh(live_source_manager, live_epg_service)
        self.main_window = MainWindow(
            browse_controller=browse_controller,
            history_controller=history_controller,
            player_controller=player_controller,
            config=config,
            save_config=lambda: self.repo.save_config(config),
            douban_controller=douban_controller,
            telegram_controller=telegram_controller,
            live_controller=live_controller,
            live_source_manager=live_source_manager,
            emby_controller=emby_controller,
            jellyfin_controller=jellyfin_controller,
            spider_plugins=spider_plugins,
            plugin_manager=self._plugin_manager,
            show_emby_tab=bool(capabilities.get("emby")),
            show_jellyfin_tab=bool(capabilities.get("jellyfin")),
        )
```

Run: `uv run pytest tests/test_app.py tests/test_api_client.py tests/test_live_epg_repository.py tests/test_live_epg_service.py tests/test_custom_live_service.py tests/test_live_source_manager_dialog.py tests/test_player_window_ui.py -v`

Expected: PASS for startup refresh, EPG persistence/service behavior, custom live integration, dialog UI, and player metadata rendering

- [ ] **Step 5: Commit the startup wiring**

```bash
git add src/atv_player/app.py tests/test_app.py
git commit -m "feat: refresh custom live epg on startup"
```

## Final Verification

**Files:**
- Test: `tests/test_app.py`
- Test: `tests/test_api_client.py`
- Test: `tests/test_custom_live_service.py`
- Test: `tests/test_live_epg_repository.py`
- Test: `tests/test_live_epg_service.py`
- Test: `tests/test_live_source_manager_dialog.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Run the targeted verification suite**

Run: `uv run pytest tests/test_app.py tests/test_api_client.py tests/test_custom_live_service.py tests/test_live_epg_repository.py tests/test_live_epg_service.py tests/test_live_source_manager_dialog.py tests/test_player_window_ui.py -v`

Expected: PASS for all EPG, startup refresh, custom-live playback, and UI coverage

- [ ] **Step 2: Run the broader live-related regression suite**

Run: `uv run pytest tests/test_live_controller.py tests/test_live_source_repository.py tests/test_live_playlist_parser.py -v`

Expected: PASS to confirm existing live-source and live-playback behavior stayed intact

- [ ] **Step 3: Commit any final cleanup**

```bash
git add src/atv_player/models.py src/atv_player/api.py src/atv_player/live_epg_repository.py src/atv_player/live_epg_service.py src/atv_player/custom_live_service.py src/atv_player/ui/live_source_manager_dialog.py src/atv_player/ui/player_window.py src/atv_player/app.py tests/test_api_client.py tests/test_live_epg_repository.py tests/test_live_epg_service.py tests/test_custom_live_service.py tests/test_live_source_manager_dialog.py tests/test_player_window_ui.py tests/test_app.py
git commit -m "test: verify custom live epg integration"
```
