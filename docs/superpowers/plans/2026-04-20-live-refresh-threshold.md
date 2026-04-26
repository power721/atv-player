# Live Refresh Threshold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show readable live-source and EPG refresh times, persist real refresh timestamps, and only auto-refresh stale live data older than four hours during app startup.

**Architecture:** Centralize refresh timestamp interpretation in `time_utils.py` so UI formatting and startup staleness checks share one definition of “valid timestamp” and “stale”. Update the live-source and EPG refresh services to write real Unix timestamps on success, then reuse the shared staleness helper from `App._start_live_background_refresh()` while keeping manual refresh actions unconditional.

**Tech Stack:** Python, PySide6, pytest

---

### Task 1: Refresh Timestamp Helpers And Dialog Formatting

**Files:**
- Modify: `src/atv_player/time_utils.py`
- Modify: `src/atv_player/ui/live_source_manager_dialog.py`
- Test: `tests/test_live_source_manager_dialog.py`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime


def test_live_source_manager_dialog_formats_source_refresh_time(qtbot) -> None:
    manager = FakeLiveSourceManager()
    manager.sources[0].last_refreshed_at = 1_713_168_000
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)

    expected = datetime.fromtimestamp(1_713_168_000).strftime("%Y-%m-%d %H:%M:%S")
    assert dialog.source_table.item(0, 5).text() == expected


def test_live_source_manager_dialog_formats_epg_refresh_time_when_no_error(qtbot) -> None:
    manager = FakeLiveSourceManager()
    manager.epg_config.last_error = ""
    manager.epg_config.last_refreshed_at = 1_713_168_000
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)

    expected = datetime.fromtimestamp(1_713_168_000).strftime("%Y-%m-%d %H:%M:%S")
    assert dialog.epg_status_label.text() == expected


def test_live_source_manager_dialog_hides_legacy_refresh_counters(qtbot) -> None:
    manager = FakeLiveSourceManager()
    manager.sources[0].last_refreshed_at = 12
    manager.epg_config.last_refreshed_at = 9
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)

    assert dialog.source_table.item(0, 5).text() == ""
    assert dialog.epg_status_label.text() == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_formats_source_refresh_time tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_formats_epg_refresh_time_when_no_error tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_hides_legacy_refresh_counters -v`
Expected: FAIL because the dialog still renders raw integers like `1713168000` and `12`.

- [ ] **Step 3: Write minimal implementation**

```python
_MIN_PLAUSIBLE_UNIX_TIMESTAMP = 946684800
_REFRESH_STALE_SECONDS = 4 * 60 * 60


def normalize_refresh_timestamp(value: object) -> int:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return 0
    timestamp = int(text)
    if timestamp >= 1_000_000_000_000:
        timestamp //= 1000
    if timestamp < _MIN_PLAUSIBLE_UNIX_TIMESTAMP:
        return 0
    return timestamp


def format_refresh_timestamp(value: object) -> str:
    timestamp = normalize_refresh_timestamp(value)
    if not timestamp:
        return ""
    return format_local_datetime(str(timestamp))


def is_refresh_stale(value: object, *, now: int | None = None) -> bool:
    timestamp = normalize_refresh_timestamp(value)
    if not timestamp:
        return True
    current = int(time.time()) if now is None else int(now)
    return current - timestamp >= _REFRESH_STALE_SECONDS
```

```python
from atv_player.time_utils import format_refresh_timestamp

def _load_epg_config(self) -> None:
    config = self.manager.load_epg_config()
    self.epg_url_edit.setPlainText(config.epg_url)
    self.epg_status_label.setText(config.last_error or format_refresh_timestamp(config.last_refreshed_at))

def reload_sources(self) -> None:
    ...
    self.source_table.setItem(row, 5, QTableWidgetItem(format_refresh_timestamp(source.last_refreshed_at)))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_formats_source_refresh_time tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_formats_epg_refresh_time_when_no_error tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_hides_legacy_refresh_counters -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/plans/2026-04-20-live-refresh-threshold.md tests/test_live_source_manager_dialog.py src/atv_player/time_utils.py src/atv_player/ui/live_source_manager_dialog.py
git commit -m "feat: format live refresh timestamps"
```

### Task 2: Persist Real Refresh Timestamps On Successful Refresh

**Files:**
- Modify: `src/atv_player/custom_live_service.py`
- Modify: `src/atv_player/live_epg_service.py`
- Test: `tests/test_custom_live_service.py`
- Test: `tests/test_live_epg_service.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_custom_live_service_refresh_source_stores_current_unix_timestamp(tmp_path: Path, monkeypatch) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    service = CustomLiveService(repo, http_client=FakeHttpClient(text="#EXTM3U\n"))
    monkeypatch.setattr("atv_player.custom_live_service.time.time", lambda: 1_713_168_000)

    service.refresh_source(source.id)

    assert repo.get_source(source.id).last_refreshed_at == 1_713_168_000


def test_custom_live_service_load_items_stores_current_unix_timestamp_when_fetching_uncached_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    service = CustomLiveService(
        repo,
        http_client=FakeHttpClient(text="#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://live.example/cctv1.m3u8\n"),
    )
    monkeypatch.setattr("atv_player.custom_live_service.time.time", lambda: 1_713_168_600)

    service.load_items(f"custom:{source.id}", 1)

    assert repo.get_source(source.id).last_refreshed_at == 1_713_168_600


def test_live_epg_service_refresh_stores_current_unix_timestamp(tmp_path: Path, monkeypatch) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_url("https://example.com/epg.xml")
    service = LiveEpgService(
        repo,
        FakeHttpBytesClient(
            payload=(
                "<tv>"
                '<channel id="c1"><display-name>CCTV-1</display-name></channel>'
                '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>朝闻天下</title></programme>'
                "</tv>"
            ).encode("utf-8")
        ),
    )
    monkeypatch.setattr("atv_player.live_epg_service.time.time", lambda: 1_713_169_200)

    service.refresh()

    assert repo.load().last_refreshed_at == 1_713_169_200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_custom_live_service.py::test_custom_live_service_refresh_source_stores_current_unix_timestamp tests/test_custom_live_service.py::test_custom_live_service_load_items_stores_current_unix_timestamp_when_fetching_uncached_source tests/test_live_epg_service.py::test_live_epg_service_refresh_stores_current_unix_timestamp -v`
Expected: FAIL because refresh success still stores counter values like `1` or `2`.

- [ ] **Step 3: Write minimal implementation**

```python
import time

def refresh_source(self, source_id: int) -> None:
    ...
    refreshed_at = int(time.time())
    self._repository.update_source(
        source.id,
        display_name=source.display_name,
        enabled=source.enabled,
        source_value=source.source_value,
        cache_text=text,
        last_error="",
        last_refreshed_at=refreshed_at,
    )

def _load_playlist(self, source) -> ParsedPlaylist:
    ...
    refreshed_at = int(time.time())
    self._repository.update_source(
        source.id,
        display_name=source.display_name,
        enabled=source.enabled,
        source_value=source.source_value,
        cache_text=text,
        last_error="",
        last_refreshed_at=refreshed_at,
    )
```

```python
import time

def refresh(self) -> None:
    ...
    refreshed_at = int(time.time())
    self._repository.save_refresh_result(
        cache_text=text,
        last_refreshed_at=refreshed_at,
        last_error="\n".join(errors),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_custom_live_service.py::test_custom_live_service_refresh_source_stores_current_unix_timestamp tests/test_custom_live_service.py::test_custom_live_service_load_items_stores_current_unix_timestamp_when_fetching_uncached_source tests/test_live_epg_service.py::test_live_epg_service_refresh_stores_current_unix_timestamp tests/test_custom_live_service.py::test_custom_live_service_refresh_source_stores_last_error_and_keeps_cache tests/test_live_epg_service.py::test_live_epg_service_refresh_preserves_old_cache_on_failure -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_custom_live_service.py tests/test_live_epg_service.py src/atv_player/custom_live_service.py src/atv_player/live_epg_service.py
git commit -m "feat: persist real live refresh timestamps"
```

### Task 3: Refresh Only Stale Live Data During Startup

**Files:**
- Modify: `src/atv_player/app.py`
- Reuse: `src/atv_player/time_utils.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_start_live_background_refresh_skips_recent_epg_and_sources(monkeypatch) -> None:
    class ImmediateThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self) -> None:
            self._target()

    class FakeRepo:
        def load_config(self):
            return AppConfig()

    class FakeEpgService:
        def __init__(self) -> None:
            self.refresh_calls = 0

        def load_config(self):
            return type(
                "Config",
                (),
                {"epg_url": "https://example.com/epg.xml", "last_refreshed_at": 1_713_168_000},
            )()

        def refresh(self) -> None:
            self.refresh_calls += 1

    class FakeLiveSourceManager:
        def __init__(self) -> None:
            self.refresh_calls: list[int] = []

        def list_sources(self):
            return [
                type("Source", (), {"id": 1, "source_type": "remote", "last_refreshed_at": 1_713_168_000})(),
                type("Source", (), {"id": 2, "source_type": "local", "last_refreshed_at": 1_713_168_000})(),
                type("Source", (), {"id": 3, "source_type": "manual", "last_refreshed_at": 1_713_168_000})(),
            ]

        def refresh_source(self, source_id: int) -> None:
            self.refresh_calls.append(source_id)

    monkeypatch.setattr(app_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr("atv_player.app.time.time", lambda: 1_713_171_000)
    coordinator = AppCoordinator(FakeRepo())
    epg_service = FakeEpgService()
    live_source_manager = FakeLiveSourceManager()

    coordinator._start_live_background_refresh(live_source_manager, epg_service)

    assert epg_service.refresh_calls == 0
    assert live_source_manager.refresh_calls == []


def test_start_live_background_refresh_refreshes_stale_epg_and_non_manual_sources(monkeypatch) -> None:
    class ImmediateThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self) -> None:
            self._target()

    class FakeRepo:
        def load_config(self):
            return AppConfig()

    class FakeEpgService:
        def __init__(self) -> None:
            self.refresh_calls = 0

        def load_config(self):
            return type(
                "Config",
                (),
                {"epg_url": "https://example.com/epg.xml", "last_refreshed_at": 12},
            )()

        def refresh(self) -> None:
            self.refresh_calls += 1

    class FakeLiveSourceManager:
        def __init__(self) -> None:
            self.refresh_calls: list[int] = []

        def list_sources(self):
            return [
                type("Source", (), {"id": 1, "source_type": "remote", "last_refreshed_at": 12})(),
                type("Source", (), {"id": 2, "source_type": "local", "last_refreshed_at": 1_713_150_000})(),
                type("Source", (), {"id": 3, "source_type": "manual", "last_refreshed_at": 0})(),
            ]

        def refresh_source(self, source_id: int) -> None:
            self.refresh_calls.append(source_id)

    monkeypatch.setattr(app_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr("atv_player.app.time.time", lambda: 1_713_171_000)
    coordinator = AppCoordinator(FakeRepo())
    epg_service = FakeEpgService()
    live_source_manager = FakeLiveSourceManager()

    coordinator._start_live_background_refresh(live_source_manager, epg_service)

    assert epg_service.refresh_calls == 1
    assert live_source_manager.refresh_calls == [1, 2]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_app.py::test_start_live_background_refresh_skips_recent_epg_and_sources tests/test_app.py::test_start_live_background_refresh_refreshes_stale_epg_and_non_manual_sources -v`
Expected: FAIL because startup refresh currently refreshes every configured EPG and every `remote` source regardless of recency, and skips `local` sources entirely.

- [ ] **Step 3: Write minimal implementation**

```python
import time

from atv_player.time_utils import is_refresh_stale

def _start_live_background_refresh(self, live_source_manager, live_epg_service) -> None:
    def refresh_epg() -> None:
        try:
            config = live_epg_service.load_config()
            if config.epg_url.strip() and is_refresh_stale(config.last_refreshed_at, now=int(time.time())):
                live_epg_service.refresh()
                logger.info("Background refresh finished target=epg")
        except Exception:
            logger.exception("Background refresh failed target=epg")
            return

    def refresh_sources() -> None:
        now = int(time.time())
        for source in live_source_manager.list_sources():
            if source.source_type == "manual":
                continue
            if not is_refresh_stale(source.last_refreshed_at, now=now):
                continue
            try:
                live_source_manager.refresh_source(source.id)
                logger.info("Background refresh finished target=live-source source_id=%s", source.id)
            except Exception:
                logger.exception("Background refresh failed target=live-source source_id=%s", source.id)
                continue
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_app.py::test_start_live_background_refresh_skips_recent_epg_and_sources tests/test_app.py::test_start_live_background_refresh_refreshes_stale_epg_and_non_manual_sources tests/test_app.py::test_app_coordinator_show_main_starts_live_background_refresh -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_app.py src/atv_player/app.py src/atv_player/time_utils.py
git commit -m "feat: skip fresh live refreshes at startup"
```

### Task 4: Full Regression Sweep

**Files:**
- Reuse: `tests/test_live_source_manager_dialog.py`
- Reuse: `tests/test_custom_live_service.py`
- Reuse: `tests/test_live_epg_service.py`
- Reuse: `tests/test_app.py`

- [ ] **Step 1: Run the focused regression suite**

```bash
uv run pytest \
  tests/test_live_source_manager_dialog.py \
  tests/test_custom_live_service.py \
  tests/test_live_epg_service.py \
  tests/test_app.py -v
```

- [ ] **Step 2: Verify the expected result**

Expected: PASS with no regressions in dialog rendering, live-source refresh persistence, EPG refresh persistence, and startup background refresh behavior.

- [ ] **Step 3: Commit the verified branch state**

```bash
git add src/atv_player/time_utils.py src/atv_player/ui/live_source_manager_dialog.py src/atv_player/custom_live_service.py src/atv_player/live_epg_service.py src/atv_player/app.py tests/test_live_source_manager_dialog.py tests/test_custom_live_service.py tests/test_live_epg_service.py tests/test_app.py
git commit -m "feat: add stale-aware live refresh timestamps"
```
