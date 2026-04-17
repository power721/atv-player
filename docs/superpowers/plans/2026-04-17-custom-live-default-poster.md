# Custom Live Default Poster Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make custom live channels without logos fall back to the bundled `src/atv_player/icons/live.png` poster in both the channel list and player request.

**Architecture:** Keep parser output raw and implement the fallback entirely inside `CustomLiveService`. Add one private helper that resolves a poster path from a merged channel view, use it when building `VodItem` and `OpenPlayerRequest`, and leave backend live channels and UI rendering code unchanged.

**Tech Stack:** Python 3, pytest, existing `CustomLiveService`, `VodItem`, and `OpenPlayerRequest` flow

---

## File Structure

### Modified Files

- `src/atv_player/custom_live_service.py`
  Add a private bundled-poster path constant plus one helper that resolves either the channel logo or the bundled default poster.
- `tests/test_custom_live_service.py`
  Add service-layer coverage for fallback behavior on `m3u` and manual custom channels, and protect explicit logos from being overwritten.

### Unchanged Files

- `src/atv_player/m3u_parser.py`
  Keep raw logo parsing unchanged.
- `src/atv_player/ui/poster_grid_page.py`
  Keep existing local-file poster rendering unchanged.
- `src/atv_player/ui/player_window.py`
  Keep existing local-file poster rendering unchanged.

## Task 1: Add Default Poster Fallback For Custom Live Channels

**Files:**
- Modify: `tests/test_custom_live_service.py`
- Modify: `src/atv_player/custom_live_service.py`
- Test: `tests/test_custom_live_service.py`

- [ ] **Step 1: Write the failing fallback tests**

```python
def test_custom_live_service_falls_back_to_default_poster_for_ungrouped_m3u_channels(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        source.id,
        display_name="自定义远程",
        enabled=True,
        source_value="https://example.com/live.m3u",
        cache_text="#EXTM3U\n#EXTINF:-1,CCTV1综合\nhttps://live.example/cctv1.m3u8\n",
        last_error="",
        last_refreshed_at=1,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    items, total = service.load_items(f"custom:{source.id}", 1)
    request = service.build_request(f"custom-channel:{source.id}:channel-0")

    assert total == 1
    assert items[0].vod_pic == str(service._DEFAULT_POSTER_PATH)
    assert request.vod.vod_pic == str(service._DEFAULT_POSTER_PATH)


def test_custom_live_service_falls_back_to_default_poster_for_grouped_manual_channels(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    service = CustomLiveService(repo, http_client=FakeHttpClient())
    source = service.add_manual_source("手动源")
    entry = service.add_manual_entry(
        source.id,
        group_name="央视频道",
        channel_name="CCTV1综合",
        stream_url="https://live.example/cctv1.m3u8",
        logo_url="",
    )

    items, total = service.load_folder_items(f"custom-folder:{source.id}:group-0")
    request = service.build_request(f"custom-channel:{source.id}:manual-{entry.id}")

    assert total == 1
    assert items[0].vod_pic == str(service._DEFAULT_POSTER_PATH)
    assert request.vod.vod_pic == str(service._DEFAULT_POSTER_PATH)


def test_custom_live_service_keeps_explicit_logo_instead_of_default_poster(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    service = CustomLiveService(repo, http_client=FakeHttpClient())
    source = service.add_manual_source("手动源")
    entry = service.add_manual_entry(
        source.id,
        group_name="",
        channel_name="CCTV1综合",
        stream_url="https://live.example/cctv1.m3u8",
        logo_url="https://img.example/cctv1.png",
    )

    items, total = service.load_items(f"custom:{source.id}", 1)
    request = service.build_request(f"custom-channel:{source.id}:manual-{entry.id}")

    assert total == 1
    assert items[0].vod_pic == "https://img.example/cctv1.png"
    assert request.vod.vod_pic == "https://img.example/cctv1.png"
```

- [ ] **Step 2: Run the fallback tests to verify they fail**

Run: `uv run pytest tests/test_custom_live_service.py::test_custom_live_service_falls_back_to_default_poster_for_ungrouped_m3u_channels tests/test_custom_live_service.py::test_custom_live_service_falls_back_to_default_poster_for_grouped_manual_channels tests/test_custom_live_service.py::test_custom_live_service_keeps_explicit_logo_instead_of_default_poster -v`

Expected: FAIL because `load_items()`, `load_folder_items()`, and `_build_request_from_channel()` currently pass through an empty `logo_url` instead of resolving the bundled fallback.

- [ ] **Step 3: Add the bundled-poster constant and resolution helper in `src/atv_player/custom_live_service.py`**

```python
class CustomLiveService:
    _DEFAULT_POSTER_PATH = Path(__file__).resolve().parent / "icons" / "live.png"

    def __init__(self, repository, http_client: _HttpTextClient) -> None:
        self._repository = repository
        self._http_client = http_client

    def _resolve_channel_poster(self, view: _MergedChannelView) -> str:
        if view.logo_url:
            return view.logo_url
        return str(self._DEFAULT_POSTER_PATH)
```

- [ ] **Step 4: Use the helper when building custom live `VodItem` rows**

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
            vod_pic=self._resolve_channel_poster(channel),
        )
        for channel in merged_channels
    ]
    return items, len(items)


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
            vod_pic=self._resolve_channel_poster(channel),
        )
        for channel in merged_channels
    ]
    return items, len(items)
```

- [ ] **Step 5: Use the helper when building custom live player requests**

```python
def _build_request_from_channel(self, view: _MergedChannelView) -> OpenPlayerRequest:
    multi_line = len(view.lines) > 1
    return OpenPlayerRequest(
        vod=VodItem(
            vod_id=view.channel_id,
            vod_name=view.channel_name,
            vod_pic=self._resolve_channel_poster(view),
            detail_style="live",
        ),
        playlist=[
            PlayItem(
                title=f"{view.channel_name} {index + 1}" if multi_line else view.channel_name,
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

- [ ] **Step 6: Run the targeted service tests to verify they pass**

Run: `uv run pytest tests/test_custom_live_service.py::test_custom_live_service_falls_back_to_default_poster_for_ungrouped_m3u_channels tests/test_custom_live_service.py::test_custom_live_service_falls_back_to_default_poster_for_grouped_manual_channels tests/test_custom_live_service.py::test_custom_live_service_keeps_explicit_logo_instead_of_default_poster -v`

Expected: PASS

- [ ] **Step 7: Run the full related regression suite**

Run: `uv run pytest tests/test_custom_live_service.py tests/test_live_controller.py -v`

Expected: PASS, confirming the custom-live fallback is limited to `CustomLiveService` and does not break live-controller routing.

- [ ] **Step 8: Commit**

```bash
git add tests/test_custom_live_service.py src/atv_player/custom_live_service.py
git commit -m "feat: add custom live default poster fallback"
```
