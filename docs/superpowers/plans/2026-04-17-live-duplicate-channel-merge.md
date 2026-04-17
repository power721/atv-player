# Live Duplicate Channel Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make duplicate custom live channels appear once in the live list while preserving every original stream URL as a switchable player line.

**Architecture:** Keep `parse_m3u()` unchanged and implement duplicate merging entirely inside `CustomLiveService`. Add a private merged-channel representation that groups parsed channels by `(group_key, channel name)`, reuse it for folder lists, ungrouped lists, and player request building, and keep per-line headers attached to each generated `PlayItem`.

**Tech Stack:** Python 3, pytest, existing `CustomLiveService`, `ParsedPlaylist`, `VodItem`, and `OpenPlayerRequest` flow

---

## File Structure

### Modified Files

- `src/atv_player/custom_live_service.py`
  Add private merged-line and merged-channel dataclasses, group parsed channels by `(group_key, channel name)`, and generate multi-line player playlists from the merged view.
- `tests/test_custom_live_service.py`
  Add focused service-layer coverage for grouped duplicates, ungrouped duplicates, group-boundary separation, manual-source duplicates, per-line headers, and single-line title behavior.

### Unchanged Files

- `src/atv_player/m3u_parser.py`
  Keep raw parsing behavior unchanged.
- `src/atv_player/controllers/live_controller.py`
  Keep the custom-source routing contract unchanged and rely on the service to return merged items and playlists.

## Task 1: Merge Duplicate Group Channels Into One Player Entry

**Files:**
- Modify: `tests/test_custom_live_service.py`
- Modify: `src/atv_player/custom_live_service.py`
- Test: `tests/test_custom_live_service.py`

- [ ] **Step 1: Write the failing grouped-duplicate tests**

```python
def test_custom_live_service_merges_duplicate_group_channels_into_one_item_and_request(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        source.id,
        display_name="自定义远程",
        enabled=True,
        source_value="https://example.com/live.m3u",
        cache_text=(
            "#EXTM3U\n"
            "#EXTINF:-1 group-title=\"央视频道\" http-header=\"Referer=https://origin-a.example/\",CCTV1综合\n"
            "https://live.example/cctv1-main.m3u8\n"
            "#EXTINF:-1 group-title=\"央视频道\" http-user-agent=\"UA-2\",CCTV1综合\n"
            "https://live.example/cctv1-backup.m3u8\n"
        ),
        last_error="",
        last_refreshed_at=1,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    items, total = service.load_folder_items(f"custom-folder:{source.id}:group-0")
    request = service.build_request(f"custom-channel:{source.id}:channel-0")

    assert total == 1
    assert [(item.vod_id, item.vod_name, item.vod_tag) for item in items] == [
        (f"custom-channel:{source.id}:channel-0", "CCTV1综合", "file")
    ]
    assert [item.title for item in request.playlist] == ["CCTV1综合 1", "CCTV1综合 2"]
    assert [item.url for item in request.playlist] == [
        "https://live.example/cctv1-main.m3u8",
        "https://live.example/cctv1-backup.m3u8",
    ]
    assert request.playlist[0].headers == {"Referer": "https://origin-a.example/"}
    assert request.playlist[1].headers == {"User-Agent": "UA-2"}


def test_custom_live_service_keeps_single_line_channel_title_without_suffix(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        source.id,
        display_name="自定义远程",
        enabled=True,
        source_value="https://example.com/live.m3u",
        cache_text="#EXTM3U\n#EXTINF:-1 group-title=\"卫视频道\",江苏卫视\nhttps://live.example/jsws.m3u8\n",
        last_error="",
        last_refreshed_at=1,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    request = service.build_request(f"custom-channel:{source.id}:channel-0")

    assert [item.title for item in request.playlist] == ["江苏卫视"]
```

- [ ] **Step 2: Run the grouped-duplicate tests to verify they fail**

Run: `uv run pytest tests/test_custom_live_service.py::test_custom_live_service_merges_duplicate_group_channels_into_one_item_and_request tests/test_custom_live_service.py::test_custom_live_service_keeps_single_line_channel_title_without_suffix -v`

Expected: FAIL because `load_folder_items(...)` still returns two `CCTV1综合` items and `build_request(...)` still produces only one `PlayItem`.

- [ ] **Step 3: Add private merged-channel helpers in `src/atv_player/custom_live_service.py`**

```python
from dataclasses import dataclass, field


@dataclass(slots=True)
class _MergedChannelLine:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    logo_url: str = ""


@dataclass(slots=True)
class _MergedChannelView:
    source_id: int
    channel_id: str
    group_key: str
    channel_name: str
    logo_url: str = ""
    lines: list[_MergedChannelLine] = field(default_factory=list)
```

- [ ] **Step 4: Implement grouped-channel merging and multi-line playlist building**

```python
def load_folder_items(self, vod_id: str) -> tuple[list[VodItem], int]:
    _prefix, source_id_text, group_key = vod_id.split(":", 2)
    source = self._repository.get_source(int(source_id_text))
    playlist = self._load_playlist(source)
    group = next(item for item in playlist.groups if item.key == group_key)
    merged_channels = self._merge_channels(source.id, group.key, group.channels)
    items = [
        VodItem(
            vod_id=f"custom-channel:{source.id}:{channel.channel_id}",
            vod_name=channel.channel_name,
            vod_tag="file",
            vod_pic=channel.logo_url,
        )
        for channel in merged_channels
    ]
    return items, len(items)


def _build_request_from_channel(self, view: _MergedChannelView) -> OpenPlayerRequest:
    multi_line = len(view.lines) > 1
    playlist = [
        PlayItem(
            title=f"{view.channel_name} {index + 1}" if multi_line else view.channel_name,
            url=line.url,
            vod_id=view.channel_id,
            index=index,
            headers=dict(line.headers),
        )
        for index, line in enumerate(view.lines)
    ]
    return OpenPlayerRequest(
        vod=VodItem(vod_id=view.channel_id, vod_name=view.channel_name, vod_pic=view.logo_url, detail_style="live"),
        playlist=playlist,
        clicked_index=0,
        source_kind="live",
        source_mode="custom",
        source_vod_id=view.channel_id,
        use_local_history=False,
    )


def _merge_channels(
    self,
    source_id: int,
    group_key: str,
    channels: list[ParsedChannel],
) -> list[_MergedChannelView]:
    merged_by_key: dict[tuple[str, str], _MergedChannelView] = {}
    merged_channels: list[_MergedChannelView] = []
    for channel in channels:
        url = channel.url.strip()
        if not url:
            continue
        merged_key = (group_key, channel.name)
        view = merged_by_key.get(merged_key)
        if view is None:
            view = _MergedChannelView(
                source_id=source_id,
                channel_id=channel.key,
                group_key=group_key,
                channel_name=channel.name,
                logo_url=channel.logo_url,
            )
            merged_by_key[merged_key] = view
            merged_channels.append(view)
        elif not view.logo_url and channel.logo_url:
            view.logo_url = channel.logo_url
        view.lines.append(
            _MergedChannelLine(
                url=url,
                headers=dict(channel.headers),
                logo_url=channel.logo_url,
            )
        )
    return merged_channels
```

- [ ] **Step 5: Update request lookup to use merged channels**

```python
def build_request(self, vod_id: str) -> OpenPlayerRequest:
    _prefix, source_id_text, channel_key = vod_id.split(":", 2)
    source = self._repository.get_source(int(source_id_text))
    playlist = self._load_playlist(source)
    for view in self._iter_merged_channel_views(source.id, playlist):
        if view.channel_id == channel_key:
            return self._build_request_from_channel(view)
    raise ValueError(f"没有可播放的项目: {vod_id}")


def _iter_merged_channel_views(self, source_id: int, playlist: ParsedPlaylist):
    for group in playlist.groups:
        yield from self._merge_channels(source_id, group.key, group.channels)
    yield from self._merge_channels(source_id, "", playlist.ungrouped_channels)
```

- [ ] **Step 6: Run the grouped-duplicate tests to verify they pass**

Run: `uv run pytest tests/test_custom_live_service.py::test_custom_live_service_merges_duplicate_group_channels_into_one_item_and_request tests/test_custom_live_service.py::test_custom_live_service_keeps_single_line_channel_title_without_suffix -v`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add tests/test_custom_live_service.py src/atv_player/custom_live_service.py
git commit -m "feat: merge duplicate grouped live channels"
```

## Task 2: Extend Merging To Ungrouped And Manual Sources And Lock Down Boundaries

**Files:**
- Modify: `tests/test_custom_live_service.py`
- Modify: `src/atv_player/custom_live_service.py`
- Test: `tests/test_custom_live_service.py`

- [ ] **Step 1: Write the failing ungrouped, cross-group, and manual-source tests**

```python
def test_custom_live_service_merges_duplicate_ungrouped_channels(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        source.id,
        display_name="自定义远程",
        enabled=True,
        source_value="https://example.com/live.m3u",
        cache_text=(
            "#EXTM3U\n"
            "#EXTINF:-1 tvg-logo=\"\",CCTV1综合\n"
            "https://live.example/cctv1-main.m3u8\n"
            "#EXTINF:-1 tvg-logo=\"https://img.example/cctv1.png\",CCTV1综合\n"
            "https://live.example/cctv1-backup.m3u8\n"
        ),
        last_error="",
        last_refreshed_at=1,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    items, total = service.load_items(f"custom:{source.id}", 1)
    request = service.build_request(f"custom-channel:{source.id}:channel-0")

    assert total == 1
    assert [(item.vod_name, item.vod_pic) for item in items] == [("CCTV1综合", "https://img.example/cctv1.png")]
    assert [item.title for item in request.playlist] == ["CCTV1综合 1", "CCTV1综合 2"]


def test_custom_live_service_does_not_merge_same_name_across_groups(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        source.id,
        display_name="自定义远程",
        enabled=True,
        source_value="https://example.com/live.m3u",
        cache_text=(
            "#EXTM3U\n"
            "#EXTINF:-1 group-title=\"央视频道\",CCTV1综合\n"
            "https://live.example/cctv1-main.m3u8\n"
            "#EXTINF:-1 group-title=\"收藏\",CCTV1综合\n"
            "https://live.example/cctv1-favorite.m3u8\n"
        ),
        last_error="",
        last_refreshed_at=1,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    root_items, root_total = service.load_items(f"custom:{source.id}", 1)
    cctv_items, cctv_total = service.load_folder_items(f"custom-folder:{source.id}:group-0")
    favorite_items, favorite_total = service.load_folder_items(f"custom-folder:{source.id}:group-1")

    assert root_total == 2
    assert [item.vod_name for item in root_items] == ["央视频道", "收藏"]
    assert cctv_total == 1
    assert favorite_total == 1
    assert [item.vod_name for item in cctv_items] == ["CCTV1综合"]
    assert [item.vod_name for item in favorite_items] == ["CCTV1综合"]


def test_custom_live_service_merges_duplicate_manual_entries_into_switchable_lines(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    service = CustomLiveService(repo, http_client=FakeHttpClient())
    source = service.add_manual_source("手动源")
    first = service.add_manual_entry(
        source.id,
        group_name="",
        channel_name="CCTV1综合",
        stream_url="https://live.example/cctv1-main.m3u8",
        logo_url="",
    )
    service.add_manual_entry(
        source.id,
        group_name="",
        channel_name="CCTV1综合",
        stream_url="https://live.example/cctv1-backup.m3u8",
        logo_url="https://img.example/cctv1.png",
    )

    items, total = service.load_items(f"custom:{source.id}", 1)
    request = service.build_request(f"custom-channel:{source.id}:manual-{first.id}")

    assert total == 1
    assert [(item.vod_name, item.vod_pic) for item in items] == [("CCTV1综合", "https://img.example/cctv1.png")]
    assert [item.title for item in request.playlist] == ["CCTV1综合 1", "CCTV1综合 2"]
    assert [item.url for item in request.playlist] == [
        "https://live.example/cctv1-main.m3u8",
        "https://live.example/cctv1-backup.m3u8",
    ]
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_custom_live_service.py::test_custom_live_service_merges_duplicate_ungrouped_channels tests/test_custom_live_service.py::test_custom_live_service_does_not_merge_same_name_across_groups tests/test_custom_live_service.py::test_custom_live_service_merges_duplicate_manual_entries_into_switchable_lines -v`

Expected: FAIL because `load_items(...)` still uses raw `playlist.ungrouped_channels`, so duplicate ungrouped and manual entries still appear as separate items.

- [ ] **Step 3: Reuse the same merge helper in `load_items(...)` and keep group boundaries intact**

```python
def load_items(self, category_id: str, page: int) -> tuple[list[VodItem], int]:
    del page
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
    merged_channels = self._merge_channels(source.id, "", playlist.ungrouped_channels)
    items = [
        VodItem(
            vod_id=f"custom-channel:{source.id}:{channel.channel_id}",
            vod_name=channel.channel_name,
            vod_tag="file",
            vod_pic=channel.logo_url,
        )
        for channel in merged_channels
    ]
    return items, len(items)
```

- [ ] **Step 4: Run the full custom-live service regression suite**

Run: `uv run pytest tests/test_custom_live_service.py tests/test_live_controller.py -v`

Expected: PASS, including the unchanged custom-channel routing test in `tests/test_live_controller.py`

- [ ] **Step 5: Commit**

```bash
git add tests/test_custom_live_service.py src/atv_player/custom_live_service.py
git commit -m "feat: merge duplicate custom live channels"
```
