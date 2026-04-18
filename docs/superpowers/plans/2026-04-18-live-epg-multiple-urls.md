# Live EPG Multiple URLs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the global custom-live EPG configuration accept multiple XMLTV URLs, merge all successful guides in order, and keep custom live playback and manual refresh working against the merged cache.

**Architecture:** Keep storage unchanged by continuing to persist the global EPG config as one row with one raw `epg_url` text blob. Put all multi-URL parsing, fetch, merge, serialization, and partial-failure handling inside `LiveEpgService`, and keep the UI change isolated to `LiveSourceManagerDialog` by switching the EPG editor from a single-line input to a normalized multi-line text editor.

**Tech Stack:** Python 3.13, PySide6, pytest, XMLTV parsing via `xml.etree.ElementTree`

---

## File Map

- Modify: `src/atv_player/live_epg_service.py`
  Responsibility: parse newline-delimited URL config, fetch multiple XMLTV payloads, merge channels/programmes in URL order, serialize deterministic cached XMLTV, and aggregate partial/full refresh errors.
- Modify: `src/atv_player/ui/live_source_manager_dialog.py`
  Responsibility: replace the single-line EPG URL editor with a multi-line editor and normalize saved URL text.
- Modify: `tests/test_live_epg_service.py`
  Responsibility: prove the current implementation fails for multi-URL refresh, then cover merge order, partial success, and full failure behavior.
- Modify: `tests/test_live_source_manager_dialog.py`
  Responsibility: cover the multi-line UI, normalized saving, and existing background refresh flow with the new widget.
- Verify only: `tests/test_live_epg_repository.py`
  Responsibility: confirm raw text persistence still works without repository code changes.
- Verify only: `tests/test_app.py`
  Responsibility: confirm startup background refresh still triggers once when the multi-line EPG config is non-empty.
- Create: `docs/superpowers/plans/2026-04-18-live-epg-multiple-urls.md`
  Responsibility: capture the implementation plan for the approved design.

## Task 1: Teach `LiveEpgService` To Merge Multiple XMLTV URLs

**Files:**
- Modify: `tests/test_live_epg_service.py`
- Modify: `src/atv_player/live_epg_service.py`
- Test: `tests/test_live_epg_service.py::test_live_epg_service_refresh_merges_multiple_urls_and_prefers_earlier_programmes`

- [ ] **Step 1: Write the failing test**

In `tests/test_live_epg_service.py`, replace `FakeHttpBytesClient` with a URL-aware version and add this regression test below `test_live_epg_service_decompresses_gzip_xmltv_payload`:

```python
class FakeHttpBytesClient:
    def __init__(
        self,
        payload: bytes = b"",
        exc: Exception | None = None,
        responses: dict[str, bytes | Exception] | None = None,
    ) -> None:
        self.payload = payload
        self.exc = exc
        self.responses = responses or {}
        self.calls: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if self.responses:
            result = self.responses[url]
            if isinstance(result, Exception):
                raise result
            return result
        if self.exc is not None:
            raise self.exc
        return self.payload


def test_live_epg_service_refresh_merges_multiple_urls_and_prefers_earlier_programmes(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_url("https://example.com/one.xml\nhttps://example.com/two.xml")
    service = LiveEpgService(
        repo,
        FakeHttpBytesClient(
            responses={
                "https://example.com/one.xml": (
                    "<tv>"
                    '<channel id="c1"><display-name>CCTV-1</display-name></channel>'
                    '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>第一源节目</title></programme>'
                    "</tv>"
                ).encode("utf-8"),
                "https://example.com/two.xml": (
                    "<tv>"
                    '<channel id="c1"><display-name>CCTV1综合</display-name></channel>'
                    '<channel id="c2"><display-name>CCTV-2</display-name></channel>'
                    '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>第二源冲突节目</title></programme>'
                    '<programme start="20260418100000 +0800" stop="20260418110000 +0800" channel="c1"><title>第二源后续节目</title></programme>'
                    '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c2"><title>经济信息联播</title></programme>'
                    "</tv>"
                ).encode("utf-8"),
            }
        ),
    )

    service.refresh()

    config = repo.load()
    c1_schedule = service.get_schedule("CCTV-1", now_text="2026-04-18T09:30:00+08:00")
    c2_schedule = service.get_schedule("CCTV-2", now_text="2026-04-18T09:30:00+08:00")

    assert service._parse_epg_urls(config.epg_url) == [
        "https://example.com/one.xml",
        "https://example.com/two.xml",
    ]
    assert "第一源节目" in config.cache_text
    assert "第二源后续节目" in config.cache_text
    assert c1_schedule is not None
    assert c1_schedule.current == "09:00-10:00 第一源节目"
    assert c1_schedule.upcoming == ["10:00-11:00 第二源后续节目"]
    assert c2_schedule is not None
    assert c2_schedule.current == "09:00-10:00 经济信息联播"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/test_live_epg_service.py::test_live_epg_service_refresh_merges_multiple_urls_and_prefers_earlier_programmes -v
```

Expected: `FAIL` because the current `refresh()` path treats the whole multi-line config as one URL and does not merge multiple XMLTV payloads.

- [ ] **Step 3: Write the minimal implementation**

Update `src/atv_player/live_epg_service.py` to parse multiple URLs, merge parsed XMLTV documents, and serialize one deterministic cache:

```python
from xml.etree import ElementTree


class LiveEpgService:
    _RESOLUTION_SUFFIX_PATTERN = re.compile(r"(hd|uhd|fhd|高清|超清|标清)+$", re.IGNORECASE)

    def refresh(self) -> None:
        config = self._repository.load()
        urls = self._parse_epg_urls(config.epg_url)
        if not urls:
            return
        errors: list[str] = []
        merged_channel_names: dict[str, list[str]] = {}
        merged_programmes: list[dict[str, object]] = []

        for url in urls:
            try:
                text = self._load_xmltv_text(url)
                channel_names_by_id, programmes = self._parse_xmltv(text)
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                continue
            merged_channel_names, merged_programmes = self._merge_xmltv(
                merged_channel_names,
                merged_programmes,
                channel_names_by_id,
                programmes,
            )

        if not merged_programmes and not merged_channel_names:
            message = "\n".join(errors) or "没有可用的 EPG URL"
            self._repository.save_refresh_result(
                cache_text=config.cache_text,
                last_refreshed_at=config.last_refreshed_at,
                last_error=message,
            )
            raise RuntimeError(message)

        cache_text = self._serialize_xmltv(merged_channel_names, merged_programmes)
        self._repository.save_refresh_result(
            cache_text=cache_text,
            last_refreshed_at=max(1, config.last_refreshed_at + 1),
            last_error="\n".join(errors),
        )

    def _parse_epg_urls(self, value: str) -> list[str]:
        urls: list[str] = []
        for line in value.splitlines():
            url = line.strip()
            if not url or url in urls:
                continue
            urls.append(url)
        return urls

    def _merge_xmltv(
        self,
        merged_channel_names: dict[str, list[str]],
        merged_programmes: list[dict[str, object]],
        channel_names_by_id: dict[str, list[str]],
        programmes: list[dict[str, object]],
    ) -> tuple[dict[str, list[str]], list[dict[str, object]]]:
        for channel_id, names in channel_names_by_id.items():
            existing_names = merged_channel_names.setdefault(channel_id, [])
            for name in names:
                if name not in existing_names:
                    existing_names.append(name)

        seen_programmes = {
            (item["channel"], item["start"], item["stop"])
            for item in merged_programmes
        }
        for item in programmes:
            key = (item["channel"], item["start"], item["stop"])
            if key in seen_programmes:
                continue
            merged_programmes.append(item)
            seen_programmes.add(key)

        merged_programmes.sort(key=lambda item: (item["channel"], item["start"]))
        return merged_channel_names, merged_programmes

    def _serialize_xmltv(
        self,
        channel_names_by_id: dict[str, list[str]],
        programmes: list[dict[str, object]],
    ) -> str:
        root = ElementTree.Element("tv")
        for channel_id in sorted(channel_names_by_id):
            channel = ElementTree.SubElement(root, "channel", {"id": channel_id})
            for name in channel_names_by_id[channel_id]:
                node = ElementTree.SubElement(channel, "display-name")
                node.text = name
        for programme in programmes:
            node = ElementTree.SubElement(
                root,
                "programme",
                {
                    "channel": str(programme["channel"]),
                    "start": programme["start"].strftime("%Y%m%d%H%M%S %z"),
                    "stop": programme["stop"].strftime("%Y%m%d%H%M%S %z"),
                },
            )
            title = ElementTree.SubElement(node, "title")
            title.text = str(programme["title"])
        return ElementTree.tostring(root, encoding="unicode")
```

Implementation constraints:

- keep `LiveEpgRepository` unchanged
- deduplicate URLs while preserving first occurrence
- let earlier URLs win for duplicate `(channel, start, stop)` programme windows
- keep cached XMLTV text parseable by the existing `get_schedule()` logic

- [ ] **Step 4: Run the focused regression slice**

Run:

```bash
uv run pytest \
  tests/test_live_epg_service.py::test_live_epg_service_decompresses_gzip_xmltv_payload \
  tests/test_live_epg_service.py::test_live_epg_service_refresh_merges_multiple_urls_and_prefers_earlier_programmes -v
```

Expected:

- both tests `PASS`
- gzip refresh still works
- multi-URL refresh now writes one merged cached XMLTV document

- [ ] **Step 5: Commit**

Run:

```bash
git add tests/test_live_epg_service.py src/atv_player/live_epg_service.py
git commit -m "feat: merge multiple live epg urls"
```

## Task 2: Preserve Cache On Full Failure And Surface Partial Failure Summaries

**Files:**
- Modify: `tests/test_live_epg_service.py`
- Modify: `src/atv_player/live_epg_service.py`
- Test: `tests/test_live_epg_service.py::test_live_epg_service_refresh_keeps_cache_when_all_urls_fail`
- Test: `tests/test_live_epg_service.py::test_live_epg_service_refresh_records_partial_failure_without_raising`

- [ ] **Step 1: Write the failing tests**

In `tests/test_live_epg_service.py`, add these tests below the merge test:

```python
import pytest


def test_live_epg_service_refresh_keeps_cache_when_all_urls_fail(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_url("https://example.com/one.xml\nhttps://example.com/two.xml")
    repo.save_refresh_result(cache_text="<tv>old</tv>", last_refreshed_at=3, last_error="")
    service = LiveEpgService(
        repo,
        FakeHttpBytesClient(
            responses={
                "https://example.com/one.xml": RuntimeError("boom-one"),
                "https://example.com/two.xml": RuntimeError("boom-two"),
            }
        ),
    )

    with pytest.raises(RuntimeError, match="boom-one"):
        service.refresh()

    config = repo.load()
    assert config.cache_text == "<tv>old</tv>"
    assert config.last_refreshed_at == 3
    assert config.last_error == (
        "https://example.com/one.xml: boom-one\n"
        "https://example.com/two.xml: boom-two"
    )


def test_live_epg_service_refresh_records_partial_failure_without_raising(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_url("https://example.com/one.xml\nhttps://example.com/two.xml")
    service = LiveEpgService(
        repo,
        FakeHttpBytesClient(
            responses={
                "https://example.com/one.xml": RuntimeError("boom-one"),
                "https://example.com/two.xml": (
                    "<tv>"
                    '<channel id="c1"><display-name>CCTV-1</display-name></channel>'
                    '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>成功节目</title></programme>'
                    "</tv>"
                ).encode("utf-8"),
            }
        ),
    )

    service.refresh()

    config = repo.load()
    schedule = service.get_schedule("CCTV-1", now_text="2026-04-18T09:30:00+08:00")

    assert "成功节目" in config.cache_text
    assert config.last_refreshed_at == 1
    assert config.last_error == "https://example.com/one.xml: boom-one"
    assert schedule is not None
    assert schedule.current == "09:00-10:00 成功节目"
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
uv run pytest \
  tests/test_live_epg_service.py::test_live_epg_service_refresh_keeps_cache_when_all_urls_fail \
  tests/test_live_epg_service.py::test_live_epg_service_refresh_records_partial_failure_without_raising -v
```

Expected:

- at least one test `FAIL`
- the current implementation either raises too early, overwrites the cache incorrectly, or does not record per-URL failures in the expected format

- [ ] **Step 3: Tighten the refresh error handling**

Refine `src/atv_player/live_epg_service.py` so full failure preserves old cache and partial failure saves merged cache without raising:

```python
    def refresh(self) -> None:
        config = self._repository.load()
        urls = self._parse_epg_urls(config.epg_url)
        if not urls:
            return
        errors: list[str] = []
        merged_channel_names: dict[str, list[str]] = {}
        merged_programmes: list[dict[str, object]] = []
        success_count = 0

        for url in urls:
            try:
                text = self._load_xmltv_text(url)
                channel_names_by_id, programmes = self._parse_xmltv(text)
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                continue
            merged_channel_names, merged_programmes = self._merge_xmltv(
                merged_channel_names,
                merged_programmes,
                channel_names_by_id,
                programmes,
            )
            success_count += 1

        if success_count == 0:
            message = "\n".join(errors) or "没有可用的 EPG URL"
            self._repository.save_refresh_result(
                cache_text=config.cache_text,
                last_refreshed_at=config.last_refreshed_at,
                last_error=message,
            )
            raise RuntimeError(message)

        self._repository.save_refresh_result(
            cache_text=self._serialize_xmltv(merged_channel_names, merged_programmes),
            last_refreshed_at=max(1, config.last_refreshed_at + 1),
            last_error="\n".join(errors),
        )
```

Keep this behavior exact:

- partial success must not raise
- full failure must raise one aggregated `RuntimeError`
- `last_error` is empty on full success, non-empty on partial success, aggregated on full failure

- [ ] **Step 4: Run the full EPG service file**

Run:

```bash
uv run pytest tests/test_live_epg_service.py -v
```

Expected: `PASS` for the full file, including existing channel matching and gzip coverage.

- [ ] **Step 5: Commit**

Run:

```bash
git add tests/test_live_epg_service.py src/atv_player/live_epg_service.py
git commit -m "test: cover live epg multi-url failures"
```

## Task 3: Convert The EPG Dialog To A Multi-Line Editor

**Files:**
- Modify: `tests/test_live_source_manager_dialog.py`
- Modify: `src/atv_player/ui/live_source_manager_dialog.py`
- Test: `tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_renders_multiline_epg_editor`
- Test: `tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_saves_normalized_multiline_epg_urls`

- [ ] **Step 1: Write the failing UI tests**

In `tests/test_live_source_manager_dialog.py`, update the fake config to start with two lines:

```python
                "epg_url": "https://example.com/epg-1.xml\nhttps://example.com/epg-2.xml",
```

Then replace the old single-line assertions with these tests:

```python
def test_live_source_manager_dialog_renders_multiline_epg_editor(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)

    assert dialog.epg_url_edit.toPlainText() == (
        "https://example.com/epg-1.xml\nhttps://example.com/epg-2.xml"
    )
    assert dialog.save_epg_button.text() == "保存"
    assert dialog.refresh_epg_button.text() == "立即更新"


def test_live_source_manager_dialog_saves_normalized_multiline_epg_urls(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.epg_url_edit.setPlainText(
        "  https://live.example/epg-1.xml  \n\n https://live.example/epg-2.xml.gz \n"
    )

    dialog._save_epg_url()

    assert manager.save_epg_url_calls == [
        "https://live.example/epg-1.xml\nhttps://live.example/epg-2.xml.gz"
    ]
```

- [ ] **Step 2: Run the failing UI slice**

Run:

```bash
uv run pytest \
  tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_renders_multiline_epg_editor \
  tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_saves_normalized_multiline_epg_urls -v
```

Expected: `FAIL` because `LiveSourceManagerDialog` still uses `QLineEdit` and `setText()/text()`.

- [ ] **Step 3: Implement the multi-line dialog behavior**

Update `src/atv_player/ui/live_source_manager_dialog.py` as follows:

```python
from PySide6.QtWidgets import QPlainTextEdit


class LiveSourceManagerDialog(QDialog):
    def __init__(self, manager, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("直播源管理")
        self.resize(920, 520)
        self._epg_refresh_thread: QThread | None = None
        self._epg_refresh_worker: _EpgRefreshWorker | None = None
        self._epg_refresh_signals = _EpgRefreshSignals(self)
        self._epg_refresh_signals.completed.connect(self._load_epg_config)
        self.epg_url_edit = QPlainTextEdit()
        self.epg_url_edit.setPlaceholderText("https://example.com/epg.xml\nhttps://example.com/backup.xml.gz")
        self.epg_url_edit.setFixedHeight(72)
        self.save_epg_button = QPushButton("保存")
        self.refresh_epg_button = QPushButton("立即更新")
        self.epg_status_label = QLabel("")
        layout = QVBoxLayout(self)
        epg_row = QHBoxLayout()
        epg_row.addWidget(QLabel("EPG URL（每行一个）"))
        epg_row.addWidget(self.epg_url_edit, 1)
        epg_row.addWidget(self.save_epg_button)
        epg_row.addWidget(self.refresh_epg_button)
        layout.addLayout(epg_row)
        layout.addWidget(self.epg_status_label)

    def _load_epg_config(self) -> None:
        config = self.manager.load_epg_config()
        self.epg_url_edit.setPlainText(config.epg_url)
        self.epg_status_label.setText(config.last_error or str(config.last_refreshed_at or ""))

    def _save_epg_url(self) -> None:
        self.manager.save_epg_url(self._normalized_epg_url_text())
        self._load_epg_config()

    def _normalized_epg_url_text(self) -> str:
        lines = []
        for line in self.epg_url_edit.toPlainText().splitlines():
            value = line.strip()
            if not value:
                continue
            lines.append(value)
        return "\n".join(lines)
```

Implementation constraints:

- only normalize whitespace and empty lines in the dialog
- keep URL deduplication in `LiveEpgService`
- keep the existing background refresh thread flow untouched

- [ ] **Step 4: Run the full dialog test file**

Run:

```bash
uv run pytest tests/test_live_source_manager_dialog.py -v
```

Expected: `PASS` for the full dialog file, including the background refresh test.

- [ ] **Step 5: Commit**

Run:

```bash
git add tests/test_live_source_manager_dialog.py src/atv_player/ui/live_source_manager_dialog.py
git commit -m "feat: add multiline live epg url editor"
```

## Task 4: Run Regression Coverage For Repository And Startup Behavior

**Files:**
- Verify only: `tests/test_live_epg_repository.py`
- Verify only: `tests/test_app.py`
- Verify only: `tests/test_live_epg_service.py`
- Verify only: `tests/test_live_source_manager_dialog.py`

- [ ] **Step 1: Verify repository persistence still passes unchanged**

Run:

```bash
uv run pytest tests/test_live_epg_repository.py -v
```

Expected: `PASS` without modifying `src/atv_player/live_epg_repository.py`, proving the repository still round-trips raw multi-line text and refresh metadata.

- [ ] **Step 2: Verify startup background refresh still works with non-empty EPG config**

Run:

```bash
uv run pytest tests/test_app.py::test_app_coordinator_starts_epg_and_remote_live_refresh_in_background -v
```

Expected: `PASS` without modifying `src/atv_player/app.py`, proving startup still triggers one background EPG refresh call and one remote live refresh call.

- [ ] **Step 3: Run the full targeted regression suite**

Run:

```bash
uv run pytest \
  tests/test_live_epg_repository.py \
  tests/test_live_epg_service.py \
  tests/test_live_source_manager_dialog.py \
  tests/test_app.py -v
```

Expected: `PASS` for all EPG repository, service, dialog, and startup coverage.

- [ ] **Step 4: Commit the completed implementation**

Run:

```bash
git add src/atv_player/live_epg_service.py src/atv_player/ui/live_source_manager_dialog.py tests/test_live_epg_service.py tests/test_live_source_manager_dialog.py
git commit -m "feat: support multiple live epg urls"
```

## Self-Review

- Spec coverage: the plan covers multi-line config input, ordered URL parsing, merged XMLTV caching, earlier-URL programme precedence, partial/full refresh failure handling, and regression verification for repository persistence plus startup refresh behavior.
- Placeholder scan: no `TODO`, `TBD`, or undefined “handle appropriately” instructions remain; each code-changing step includes concrete test code, implementation code, commands, and expected outcomes.
- Type consistency: the plan keeps `LiveEpgConfig.epg_url` as `str`, introduces `_parse_epg_urls()`, `_merge_xmltv()`, and `_serialize_xmltv()` only inside `LiveEpgService`, and switches the UI to `QPlainTextEdit` with `toPlainText()/setPlainText()` consistently throughout the dialog tests and implementation.
