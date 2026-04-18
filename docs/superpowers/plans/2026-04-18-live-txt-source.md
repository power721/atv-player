# Live TXT Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add support for the approved `txt` live source format for both remote URLs and local files while preserving the existing duplicate-channel merge behavior.

**Architecture:** Add a small live-playlist parser entrypoint that dispatches between raw `m3u` parsing and the approved `txt` syntax, then switch `CustomLiveService` to use that entrypoint instead of calling `parse_m3u()` directly. Keep the duplicate-line merge behavior in `CustomLiveService` unchanged so `txt` sources automatically gain grouped multi-line playback once they are normalized into `ParsedPlaylist`.

**Tech Stack:** Python 3, pytest, existing `CustomLiveService`, `ParsedPlaylist` models, Qt dialog tests

---

## File Structure

### Created Files

- `src/atv_player/live_playlist_parser.py`
  Add `parse_live_playlist()` and a dedicated `txt` parser that returns the existing `ParsedPlaylist` model.
- `tests/test_live_playlist_parser.py`
  Add focused parser tests for grouped `txt`, ungrouped `txt`, malformed rows, and `m3u` dispatch behavior.

### Modified Files

- `src/atv_player/custom_live_service.py`
  Replace direct `parse_m3u()` calls with `parse_live_playlist()`.
- `src/atv_player/ui/live_source_manager_dialog.py`
  Broaden the remote prompt label and local file-picker filter.
- `tests/test_custom_live_service.py`
  Add service coverage for local `txt` files and duplicate-channel merging from `txt` content.
- `tests/test_live_source_manager_dialog.py`
  Assert the UI prompt label and local-file filter include `txt`.

### Unchanged Files

- `src/atv_player/m3u_parser.py`
  Keep raw `m3u` parsing behavior as-is.

## Task 1: Add The Unified Playlist Parser Entry Point

**Files:**
- Create: `src/atv_player/live_playlist_parser.py`
- Create: `tests/test_live_playlist_parser.py`
- Test: `tests/test_live_playlist_parser.py`

- [ ] **Step 1: Write the failing parser dispatch tests**

```python
from atv_player.live_playlist_parser import parse_live_playlist


def test_parse_live_playlist_dispatches_extm3u_to_m3u_parser() -> None:
    playlist = """#EXTM3U
#EXTINF:-1 group-title="央视频道",CCTV-1综合
https://live.example/cctv1.m3u8
"""

    parsed = parse_live_playlist(playlist)

    assert [group.name for group in parsed.groups] == ["央视频道"]
    assert [(item.name, item.url) for item in parsed.groups[0].channels] == [
        ("CCTV-1综合", "https://live.example/cctv1.m3u8")
    ]


def test_parse_live_playlist_parses_txt_group_rows_and_duplicate_channels() -> None:
    playlist = """🇨🇳IPV4线路,#genre#
CCTV-1,http://107.150.60.122/live/cctv1hd.m3u8
CCTV-1,http://63.141.230.178:82/gslb/zbdq5.m3u8?id=cctv1hd
"""

    parsed = parse_live_playlist(playlist)

    assert [group.name for group in parsed.groups] == ["🇨🇳IPV4线路"]
    assert [(item.name, item.url) for item in parsed.groups[0].channels] == [
        ("CCTV-1", "http://107.150.60.122/live/cctv1hd.m3u8"),
        ("CCTV-1", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=cctv1hd"),
    ]
```

- [ ] **Step 2: Run the focused parser tests to verify they fail**

Run: `uv run pytest tests/test_live_playlist_parser.py -v`

Expected: FAIL with `ModuleNotFoundError` because `atv_player.live_playlist_parser` does not exist yet

- [ ] **Step 3: Create the parser entrypoint and dispatch logic**

```python
from __future__ import annotations

from atv_player.m3u_parser import ParsedPlaylist, parse_m3u


def parse_live_playlist(text: str) -> ParsedPlaylist:
    if text.lstrip().startswith("#EXTM3U"):
        return parse_m3u(text)
    return _parse_txt_playlist(text)
```

- [ ] **Step 4: Add the approved `txt` parser implementation**

```python
from atv_player.m3u_parser import ParsedChannel, ParsedGroup, ParsedPlaylist


def _parse_txt_playlist(text: str) -> ParsedPlaylist:
    result = ParsedPlaylist()
    current_group_name = ""
    groups_by_name: dict[str, ParsedGroup] = {}
    channel_index = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "," not in line:
            continue
        name, value = [part.strip() for part in line.split(",", 1)]
        if not name or not value:
            continue
        if value == "#genre#":
            current_group_name = name
            if current_group_name and current_group_name not in groups_by_name:
                group = ParsedGroup(key=f"group-{len(groups_by_name)}", name=current_group_name)
                groups_by_name[current_group_name] = group
                result.groups.append(group)
            continue
        channel = ParsedChannel(
            key=f"channel-{channel_index}",
            name=name,
            url=value,
        )
        channel_index += 1
        if current_group_name:
            groups_by_name[current_group_name].channels.append(channel)
        else:
            result.ungrouped_channels.append(channel)
    return result
```

- [ ] **Step 5: Run the parser tests to verify they pass**

Run: `uv run pytest tests/test_live_playlist_parser.py -v`

Expected: PASS for both dispatch and grouped `txt` parsing tests

- [ ] **Step 6: Commit the parser entrypoint**

```bash
git add src/atv_player/live_playlist_parser.py tests/test_live_playlist_parser.py
git commit -m "feat: add live txt playlist parser"
```

## Task 2: Harden TXT Parsing Rules

**Files:**
- Modify: `tests/test_live_playlist_parser.py`
- Modify: `src/atv_player/live_playlist_parser.py`
- Test: `tests/test_live_playlist_parser.py`

- [ ] **Step 1: Write the failing edge-case parser tests**

```python
def test_parse_live_playlist_keeps_txt_channels_ungrouped_before_first_group() -> None:
    playlist = """CGTN,http://live.example/cgtn.m3u8
🇨🇳IPV4线路,#genre#
CCTV-1,http://live.example/cctv1.m3u8
"""

    parsed = parse_live_playlist(playlist)

    assert [(item.name, item.url) for item in parsed.ungrouped_channels] == [
        ("CGTN", "http://live.example/cgtn.m3u8")
    ]
    assert [group.name for group in parsed.groups] == ["🇨🇳IPV4线路"]


def test_parse_live_playlist_ignores_blank_comments_and_malformed_txt_rows() -> None:
    playlist = """
# comment
无效行
卫视频道,#genre#
,
湖南卫视,
湖南卫视,http://live.example/hunan.m3u8
"""

    parsed = parse_live_playlist(playlist)

    assert parsed.ungrouped_channels == []
    assert [group.name for group in parsed.groups] == ["卫视频道"]
    assert [(item.name, item.url) for item in parsed.groups[0].channels] == [
        ("湖南卫视", "http://live.example/hunan.m3u8")
    ]
```

- [ ] **Step 2: Run the focused edge-case tests to verify they fail**

Run: `uv run pytest tests/test_live_playlist_parser.py::test_parse_live_playlist_keeps_txt_channels_ungrouped_before_first_group tests/test_live_playlist_parser.py::test_parse_live_playlist_ignores_blank_comments_and_malformed_txt_rows -v`

Expected: FAIL until the parser handles ungrouped rows and malformed-row filtering exactly as specified

- [ ] **Step 3: Adjust the parser to preserve ungrouped rows and ignore malformed input**

```python
def _parse_txt_playlist(text: str) -> ParsedPlaylist:
    result = ParsedPlaylist()
    current_group_name = ""
    groups_by_name: dict[str, ParsedGroup] = {}
    channel_index = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "," not in line:
            continue
        name, value = [part.strip() for part in line.split(",", 1)]
        if not name or not value:
            continue
        if value == "#genre#":
            current_group_name = name
            if current_group_name not in groups_by_name:
                group = ParsedGroup(key=f"group-{len(groups_by_name)}", name=current_group_name)
                groups_by_name[current_group_name] = group
                result.groups.append(group)
            continue
        channel = ParsedChannel(key=f"channel-{channel_index}", name=name, url=value)
        channel_index += 1
        group = groups_by_name.get(current_group_name)
        if group is None:
            result.ungrouped_channels.append(channel)
        else:
            group.channels.append(channel)
    return result
```

- [ ] **Step 4: Run the full parser suite to verify it passes**

Run: `uv run pytest tests/test_live_playlist_parser.py -v`

Expected: PASS for grouped, ungrouped, and malformed-row parser coverage

- [ ] **Step 5: Commit the parser hardening**

```bash
git add src/atv_player/live_playlist_parser.py tests/test_live_playlist_parser.py
git commit -m "test: cover live txt parser edge cases"
```

## Task 3: Route Custom Live Sources Through The Unified Parser

**Files:**
- Modify: `src/atv_player/custom_live_service.py`
- Modify: `tests/test_custom_live_service.py`
- Test: `tests/test_custom_live_service.py`

- [ ] **Step 1: Write the failing service tests for local `txt` loading and merged duplicate lines**

```python
def test_custom_live_service_loads_local_txt_source_and_lists_groups(tmp_path: Path) -> None:
    playlist_path = tmp_path / "iptv.txt"
    playlist_path.write_text(
        "🇨🇳IPV4线路,#genre#\n"
        "CCTV-1,http://live.example/cctv1-main.m3u8\n"
        "CCTV-2,http://live.example/cctv2.m3u8\n",
        encoding="utf-8",
    )
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("local", str(playlist_path), "本地 TXT")
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    items, total = service.load_items(f"custom:{source.id}", 1)

    assert total == 1
    assert [(item.vod_id, item.vod_name, item.vod_tag) for item in items] == [
        (f"custom-folder:{source.id}:group-0", "🇨🇳IPV4线路", "folder")
    ]


def test_custom_live_service_merges_duplicate_txt_channels_into_switchable_lines(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.txt", "远程 TXT")
    repo.update_source(
        source.id,
        display_name="远程 TXT",
        enabled=True,
        source_value="https://example.com/live.txt",
        cache_text=(
            "🇨🇳IPV4线路,#genre#\n"
            "CCTV-1,http://live.example/cctv1-main.m3u8\n"
            "CCTV-1,http://live.example/cctv1-backup.m3u8\n"
        ),
        last_error="",
        last_refreshed_at=1,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    items, total = service.load_folder_items(f"custom-folder:{source.id}:group-0")
    request = service.build_request(f"custom-channel:{source.id}:channel-0")

    assert total == 1
    assert [item.vod_name for item in items] == ["CCTV-1"]
    assert [item.title for item in request.playlist] == ["CCTV-1 1", "CCTV-1 2"]
    assert [item.url for item in request.playlist] == [
        "http://live.example/cctv1-main.m3u8",
        "http://live.example/cctv1-backup.m3u8",
    ]
```

- [ ] **Step 2: Run the focused service tests to verify they fail**

Run: `uv run pytest tests/test_custom_live_service.py::test_custom_live_service_loads_local_txt_source_and_lists_groups tests/test_custom_live_service.py::test_custom_live_service_merges_duplicate_txt_channels_into_switchable_lines -v`

Expected: FAIL because `CustomLiveService` still calls `parse_m3u()` directly and cannot parse `txt`

- [ ] **Step 3: Switch `CustomLiveService` to the unified parser entrypoint**

```python
from atv_player.live_playlist_parser import parse_live_playlist


def _load_playlist(self, source) -> ParsedPlaylist:
    if source.source_type == "manual":
        return self._load_manual_playlist(source.id)
    if source.cache_text:
        return parse_live_playlist(source.cache_text)
    text = self._read_source_text(source)
    self._repository.update_source(
        source.id,
        display_name=source.display_name,
        enabled=source.enabled,
        source_value=source.source_value,
        cache_text=text,
        last_error="",
        last_refreshed_at=max(1, source.last_refreshed_at + 1),
    )
    return parse_live_playlist(text)
```

- [ ] **Step 4: Run the custom live service suite to verify existing merge behavior still passes**

Run: `uv run pytest tests/test_custom_live_service.py -v`

Expected: PASS for existing `m3u`, manual-source, and new `txt`-source tests

- [ ] **Step 5: Commit the service integration**

```bash
git add src/atv_player/custom_live_service.py tests/test_custom_live_service.py
git commit -m "feat: support txt custom live sources"
```

## Task 4: Broaden The Live Source Manager UI

**Files:**
- Modify: `src/atv_player/ui/live_source_manager_dialog.py`
- Modify: `tests/test_live_source_manager_dialog.py`
- Test: `tests/test_live_source_manager_dialog.py`

- [ ] **Step 1: Write the failing dialog tests**

```python
def test_live_source_manager_dialog_prompts_for_generic_live_source_url(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    asked = {}

    def fake_get_text(parent, title, label, **kwargs):
        asked["title"] = title
        asked["label"] = label
        return "https://example.com/live.txt", True

    monkeypatch.setattr("atv_player.ui.live_source_manager_dialog.QInputDialog.getText", fake_get_text)

    assert dialog._prompt_remote_source() == "https://example.com/live.txt"
    assert asked == {"title": "添加远程源", "label": "直播源 URL"}


def test_live_source_manager_dialog_local_picker_accepts_txt_files(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    asked = {}

    def fake_pick(parent, title, directory, file_filter):
        asked["title"] = title
        asked["filter"] = file_filter
        return "/tmp/iptv.txt", "TXT Files (*.txt)"

    monkeypatch.setattr("atv_player.ui.live_source_manager_dialog.QFileDialog.getOpenFileName", fake_pick)

    assert dialog._pick_local_source() == "/tmp/iptv.txt"
    assert asked == {
        "title": "选择直播源文件",
        "filter": "Live Source Files (*.m3u *.m3u8 *.txt)",
    }
```

- [ ] **Step 2: Run the focused dialog tests to verify they fail**

Run: `uv run pytest tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_prompts_for_generic_live_source_url tests/test_live_source_manager_dialog.py::test_live_source_manager_dialog_local_picker_accepts_txt_files -v`

Expected: FAIL because the dialog still uses `M3U URL` and excludes `*.txt`

- [ ] **Step 3: Update the prompt label and file filter**

```python
def _prompt_remote_source(self) -> str:
    url, accepted = QInputDialog.getText(self, "添加远程源", "直播源 URL")
    return url.strip() if accepted else ""


def _pick_local_source(self) -> str:
    path, _ = QFileDialog.getOpenFileName(
        self,
        "选择直播源文件",
        "",
        "Live Source Files (*.m3u *.m3u8 *.txt)",
    )
    return path.strip()
```

- [ ] **Step 4: Run the dialog suite to verify it passes**

Run: `uv run pytest tests/test_live_source_manager_dialog.py -v`

Expected: PASS for the new prompt/filter assertions and the existing source-manager dialog tests

- [ ] **Step 5: Commit the UI update**

```bash
git add src/atv_player/ui/live_source_manager_dialog.py tests/test_live_source_manager_dialog.py
git commit -m "feat: allow txt live source selection"
```

## Task 5: Final Verification

**Files:**
- Modify: `src/atv_player/live_playlist_parser.py`
- Modify: `src/atv_player/custom_live_service.py`
- Modify: `src/atv_player/ui/live_source_manager_dialog.py`
- Test: `tests/test_live_playlist_parser.py`
- Test: `tests/test_custom_live_service.py`
- Test: `tests/test_live_source_manager_dialog.py`

- [ ] **Step 1: Run the focused verification suites together**

Run: `uv run pytest tests/test_live_playlist_parser.py tests/test_custom_live_service.py tests/test_live_source_manager_dialog.py -v`

Expected: PASS for parser, service, and UI coverage

- [ ] **Step 2: Run a final diff review before completion**

Run: `git diff -- src/atv_player/live_playlist_parser.py src/atv_player/custom_live_service.py src/atv_player/ui/live_source_manager_dialog.py tests/test_live_playlist_parser.py tests/test_custom_live_service.py tests/test_live_source_manager_dialog.py`

Expected: Only the planned parser, service, UI, and test changes appear

## Self-Review

- Spec coverage:
  `txt` parser support is covered by Tasks 1 and 2.
  `CustomLiveService` integration for remote and local sources is covered by Task 3.
  Duplicate-channel merge reuse for `txt` sources is covered by Task 3.
  UI wording and file-filter updates are covered by Task 4.
- Placeholder scan:
  No placeholder text remains.
- Type consistency:
  The plan consistently uses `parse_live_playlist()`, `ParsedPlaylist`, and existing `CustomLiveService` APIs.
