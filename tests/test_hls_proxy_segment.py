from atv_player.proxy.segment import SegmentProxy
from atv_player.proxy.session import PlaylistSegment, ProxySessionRegistry


def test_segment_proxy_repairs_bytes_and_reuses_cache() -> None:
    requests: list[str] = []

    class FakeResponse:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool) -> FakeResponse:
        requests.append(url)
        return FakeResponse(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 20 + b"IEND\xaeB`\x82" + (b"\x47" + b"\x00" * 187)
        )

    registry = ProxySessionRegistry()
    token = registry.create_session("https://media.example/path/index.m3u8", {"Referer": "https://site.example"})
    registry.get(token).segments = [
        PlaylistSegment(index=0, url="https://media.example/path/0001.png", duration=5.0)
    ]
    proxy = SegmentProxy(session_registry=registry, get=fake_get)

    first = proxy.fetch_segment(token, 0)
    second = proxy.fetch_segment(token, 0)

    assert first.startswith(b"\x47")
    assert second == first
    assert requests == ["https://media.example/path/0001.png"]


def test_segment_proxy_schedules_prefetch_for_next_segments() -> None:
    scheduled: list[tuple[str, int]] = []

    registry = ProxySessionRegistry()
    token = registry.create_session("https://media.example/path/index.m3u8", {})
    registry.get(token).segments = [
        PlaylistSegment(index=0, url="https://media.example/path/0001.ts", duration=5.0),
        PlaylistSegment(index=1, url="https://media.example/path/0002.ts", duration=5.0),
        PlaylistSegment(index=2, url="https://media.example/path/0003.ts", duration=5.0),
    ]
    proxy = SegmentProxy(session_registry=registry)
    proxy._prefetch_segment = lambda session_token, segment_index: scheduled.append((session_token, segment_index))

    proxy.schedule_prefetch(token, 0)

    assert scheduled == [(token, 1), (token, 2)]


def test_segment_proxy_prefetch_does_not_recursively_prefetch_full_playlist() -> None:
    requests: list[str] = []

    class FakeResponse:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool) -> FakeResponse:
        requests.append(url)
        return FakeResponse(b"\x47" + url.encode("utf-8"))

    registry = ProxySessionRegistry()
    token = registry.create_session("https://media.example/path/index.m3u8", {})
    registry.get(token).segments = [
        PlaylistSegment(index=0, url="https://media.example/path/0001.ts", duration=5.0),
        PlaylistSegment(index=1, url="https://media.example/path/0002.ts", duration=5.0),
        PlaylistSegment(index=2, url="https://media.example/path/0003.ts", duration=5.0),
        PlaylistSegment(index=3, url="https://media.example/path/0004.ts", duration=5.0),
        PlaylistSegment(index=4, url="https://media.example/path/0005.ts", duration=5.0),
    ]
    proxy = SegmentProxy(session_registry=registry, get=fake_get)
    proxy._prefetch_segment = lambda session_token, segment_index: proxy.fetch_segment(
        session_token,
        segment_index,
        prefetch=True,
    )

    proxy.fetch_segment(token, 0)

    assert requests == [
        "https://media.example/path/0001.ts",
        "https://media.example/path/0002.ts",
        "https://media.example/path/0003.ts",
    ]


def test_segment_proxy_uses_session_headers_for_asset_fetch() -> None:
    seen_headers: list[dict[str, str]] = []

    class FakeResponse:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool) -> FakeResponse:
        seen_headers.append(headers)
        return FakeResponse(b"key-bytes")

    registry = ProxySessionRegistry()
    token = registry.create_session("https://media.example/path/index.m3u8", {"Referer": "https://site.example"})
    proxy = SegmentProxy(session_registry=registry, get=fake_get)

    payload = proxy.fetch_asset(token, "https://media.example/path/key.bin")

    assert payload == b"key-bytes"
    assert seen_headers == [{"Referer": "https://site.example"}]
