# Live M3U HTTP Headers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parse custom HTTP headers from custom live `m3u` playlists and pass them into playback through `PlayItem.headers`.

**Architecture:** Extend `m3u_parser` so `ParsedChannel` carries parsed headers, then update `CustomLiveService` to copy those headers into the `PlayItem` it builds for custom live playback. Reuse the existing player header path rather than changing mpv integration.

**Tech Stack:** Python 3, pytest, existing custom live service and mpv header handling

---

## File Structure

### Modified Files

- `src/atv_player/m3u_parser.py`
  Add parsed header support for `http-user-agent` and `http-header`.
- `src/atv_player/custom_live_service.py`
  Pass parsed headers into `PlayItem.headers` during custom live playback request construction.
- `tests/test_m3u_parser.py`
  Add coverage for `http-user-agent`, `http-header`, and malformed header segments.
- `tests/test_custom_live_service.py`
  Assert custom playback requests include the parsed headers.

## Task 1: Parse HTTP Headers From M3U Metadata

**Files:**
- Modify: `src/atv_player/m3u_parser.py`
- Test: `tests/test_m3u_parser.py`

- [ ] **Step 1: Write the failing parser tests**

```python
def test_parse_m3u_parses_http_user_agent_and_http_headers() -> None:
    playlist = """#EXTM3U
#EXTINF:-1 http-user-agent="AptvPlayer-UA" http-header="Referer=https://site.example/&Origin=https://origin.example" group-title="卫视",江苏卫视
https://live.example/jsws.m3u8
"""

    parsed = parse_m3u(playlist)

    assert parsed.groups[0].channels[0].headers == {
        "User-Agent": "AptvPlayer-UA",
        "Referer": "https://site.example/",
        "Origin": "https://origin.example",
    }


def test_parse_m3u_ignores_malformed_http_header_segments() -> None:
    playlist = """#EXTM3U
#EXTINF:-1 http-header="broken&Referer=https://site.example/" ,测试台
https://live.example/test.m3u8
"""

    parsed = parse_m3u(playlist)

    assert parsed.ungrouped_channels[0].headers == {
        "Referer": "https://site.example/",
    }
```

- [ ] **Step 2: Run the parser tests to verify they fail**

Run: `uv run pytest tests/test_m3u_parser.py -v`

Expected: FAIL because `ParsedChannel` does not yet expose `headers`

- [ ] **Step 3: Extend `ParsedChannel` and add header parsing helpers**

```python
@dataclass(slots=True)
class ParsedChannel:
    key: str
    name: str
    url: str
    logo_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def _parse_http_headers(attrs: dict[str, str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    user_agent = attrs.get("http-user-agent", "").strip()
    if user_agent:
        headers["User-Agent"] = user_agent
    raw_header = attrs.get("http-header", "").strip()
    if not raw_header:
        return headers
    for part in raw_header.split("&"):
        chunk = part.strip()
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        headers[key] = value
    return headers
```

- [ ] **Step 4: Wire parsed headers into `parse_m3u()`**

```python
        channel = ParsedChannel(
            key=f"channel-{channel_index}",
            name=pending_name,
            url=line,
            logo_url=pending_logo,
            headers=_parse_http_headers(attrs),
        )
```

- [ ] **Step 5: Run the parser tests to verify they pass**

Run: `uv run pytest tests/test_m3u_parser.py -v`

Expected: PASS for existing parser tests and the new header tests

- [ ] **Step 6: Commit the parser update**

```bash
git add src/atv_player/m3u_parser.py tests/test_m3u_parser.py
git commit -m "feat: parse live m3u http headers"
```

## Task 2: Pass Parsed Headers Into Playback

**Files:**
- Modify: `src/atv_player/custom_live_service.py`
- Test: `tests/test_custom_live_service.py`

- [ ] **Step 1: Write the failing service test**

```python
def test_custom_live_service_build_request_copies_channel_headers(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        source.id,
        display_name="自定义远程",
        enabled=True,
        source_value="https://example.com/live.m3u",
        cache_text=(
            "#EXTM3U\n"
            "#EXTINF:-1 http-user-agent=\"AptvPlayer-UA\" "
            "http-header=\"Referer=https://site.example/&Origin=https://origin.example\",江苏卫视\n"
            "https://live.example/jsws.m3u8\n"
        ),
        last_error="",
        last_refreshed_at=1,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    request = service.build_request(f"custom-channel:{source.id}:channel-0")

    assert request.playlist[0].headers == {
        "User-Agent": "AptvPlayer-UA",
        "Referer": "https://site.example/",
        "Origin": "https://origin.example",
    }
```

- [ ] **Step 2: Run the focused service test to verify it fails**

Run: `uv run pytest tests/test_custom_live_service.py::test_custom_live_service_build_request_copies_channel_headers -v`

Expected: FAIL because headers are not yet copied into `PlayItem`

- [ ] **Step 3: Add headers to the channel view and request construction**

```python
yield LiveSourceChannelView(
    source_id=source_id,
    channel_id=channel.key,
    group_key="",
    channel_name=channel.name,
    stream_url=channel.url,
    logo_url=channel.logo_url,
    headers=dict(channel.headers),
)
```

```python
playlist=[PlayItem(
    title=view.channel_name,
    url=view.stream_url,
    vod_id=view.channel_id,
    index=0,
    headers=dict(view.headers),
)]
```

- [ ] **Step 4: Run the focused service and parser suites**

Run: `uv run pytest tests/test_m3u_parser.py tests/test_custom_live_service.py -v`

Expected: PASS with parsed headers reaching `PlayItem.headers`

- [ ] **Step 5: Commit the playback-header propagation**

```bash
git add src/atv_player/custom_live_service.py tests/test_custom_live_service.py
git commit -m "feat: pass live m3u headers to playback"
```

## Self-Review

- Spec coverage:
  Parser support for `http-user-agent` and `http-header` is covered by Task 1.
  Playback propagation through `PlayItem.headers` is covered by Task 2.
- Placeholder scan:
  No placeholders remain.
- Type consistency:
  The plan uses `ParsedChannel.headers` and `PlayItem.headers` consistently across parser and service tasks.
