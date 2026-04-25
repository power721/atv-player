import errno

from atv_player.player.m3u8_ad_filter import M3U8AdFilter
from atv_player.proxy.server import LocalHlsProxyServer
from atv_player.proxy.session import PlaylistSegment
import httpx


def test_m3u8_ad_filter_returns_proxy_url_for_remote_m3u8() -> None:
    class FakeServer:
        def start(self) -> None:
            return None

        def create_playlist_url(self, url: str, headers: dict[str, str] | None = None) -> str:
            assert headers == {"Referer": "https://site.example"}
            return "http://127.0.0.1:2323/m3u?v=test-token"

        def close(self) -> None:
            return None

    ad_filter = M3U8AdFilter(proxy_server=FakeServer())

    prepared = ad_filter.prepare(
        "https://media.example/path/index.m3u8",
        {"Referer": "https://site.example"},
    )

    assert prepared == "http://127.0.0.1:2323/m3u?v=test-token"


def test_m3u8_ad_filter_leaves_non_m3u8_url_unchanged() -> None:
    ad_filter = M3U8AdFilter()

    assert ad_filter.should_prepare("https://media.example/video.mp4") is False


def test_local_hls_proxy_server_returns_404_for_missing_token() -> None:
    server = LocalHlsProxyServer()

    status, headers, body = server.handle_request("GET", "/m3u?v=missing")

    assert status == 404
    assert headers == []
    assert body == b"missing proxy session"


def test_local_hls_proxy_server_returns_502_when_playlist_fetch_fails() -> None:
    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool):
        raise RuntimeError("origin down")

    server = LocalHlsProxyServer(get=fake_get)
    playlist_url = server.create_playlist_url("https://media.example/path/index.m3u8", {})
    token = playlist_url.rsplit("=", 1)[-1]

    status, headers, body = server.handle_request("GET", f"/m3u?v={token}")

    assert status == 502
    assert headers == []
    assert body == b"origin down"


def test_local_hls_proxy_server_deletes_session_when_playlist_returns_403() -> None:
    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool):
        request = httpx.Request("GET", url)
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    server = LocalHlsProxyServer(get=fake_get)
    playlist_url = server.create_playlist_url("https://media.example/path/index.m3u8", {})
    token = playlist_url.rsplit("=", 1)[-1]

    status, headers, body = server.handle_request("GET", f"/m3u?v={token}")

    assert status == 502
    assert headers == []
    assert body == b"forbidden"
    assert server._registry.contains(token) is False


def test_local_hls_proxy_server_reuses_cached_playlist_when_origin_m3u8_becomes_403() -> None:
    requests: list[str] = []

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    playlist_text = """#EXTM3U
#EXTINF:5.0,
segment-0001.ts
"""
    origin_url = "https://media.example/path/index.m3u8"

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool):
        requests.append(url)
        if len(requests) == 1:
            return FakeResponse(playlist_text)
        request = httpx.Request("GET", url)
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    server = LocalHlsProxyServer(get=fake_get)
    playlist_url = server.create_playlist_url(origin_url, {})
    token = playlist_url.rsplit("=", 1)[-1]

    first_status, first_headers, first_body = server.handle_request("GET", f"/m3u?v={token}")
    second_status, second_headers, second_body = server.handle_request("GET", f"/m3u?v={token}")

    expected_body = (
        "#EXTM3U\n#EXTINF:5.0,\nhttp://127.0.0.1:2323/seg?v="
        f"{token}&i=0\n"
    ).encode("utf-8")

    assert first_status == 200
    assert first_headers == [("Content-Type", "application/vnd.apple.mpegurl")]
    assert first_body == expected_body
    assert second_status == 200
    assert second_headers == [("Content-Type", "application/vnd.apple.mpegurl")]
    assert second_body == expected_body
    assert requests == [origin_url, origin_url]
    assert server._registry.contains(token) is True


def test_local_hls_proxy_server_returns_segment_for_v_query_param() -> None:
    class FakeResponse:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool):
        return FakeResponse((b"\x47" + b"\x00" * 187) * 2)

    server = LocalHlsProxyServer(get=fake_get)
    playlist_url = server.create_playlist_url("https://media.example/path/index.m3u8", {})
    token = playlist_url.rsplit("=", 1)[-1]
    server._registry.get(token).segments = [
        PlaylistSegment(index=0, url="https://media.example/path/segment0.ts", duration=5.0)
    ]

    status, headers, body = server.handle_request("GET", f"/seg?v={token}&i=0")

    assert status == 200
    assert headers == [("Content-Type", "video/MP2T")]
    assert body.startswith(b"\x47")


def test_local_hls_proxy_server_falls_back_to_ephemeral_port_when_default_port_is_busy(monkeypatch) -> None:
    bind_attempts: list[tuple[str, int]] = []

    class FakeThreadingHTTPServer:
        def __init__(self, server_address: tuple[str, int], handler) -> None:
            del handler
            bind_attempts.append(server_address)
            if server_address[1] == 2323:
                raise OSError(errno.EADDRINUSE, "Address already in use")
            self.server_address = (server_address[0], 45123)

        def serve_forever(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    monkeypatch.setattr("atv_player.proxy.server.ThreadingHTTPServer", FakeThreadingHTTPServer)

    server = LocalHlsProxyServer()

    server.start()
    prepared = server.create_playlist_url("https://media.example/path/index.m3u8", {})
    server.close()

    assert bind_attempts == [("127.0.0.1", 2323), ("127.0.0.1", 0)]
    assert prepared.startswith("http://127.0.0.1:45123/m3u?v=")
