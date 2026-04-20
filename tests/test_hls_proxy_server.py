from atv_player.player.m3u8_ad_filter import M3U8AdFilter
from atv_player.proxy.server import LocalHlsProxyServer


def test_m3u8_ad_filter_returns_proxy_url_for_remote_m3u8() -> None:
    class FakeServer:
        def start(self) -> None:
            return None

        def create_playlist_url(self, url: str, headers: dict[str, str] | None = None) -> str:
            assert headers == {"Referer": "https://site.example"}
            return "http://127.0.0.1:2323/m3u?token=test-token"

        def close(self) -> None:
            return None

    ad_filter = M3U8AdFilter(proxy_server=FakeServer())

    prepared = ad_filter.prepare(
        "https://media.example/path/index.m3u8",
        {"Referer": "https://site.example"},
    )

    assert prepared == "http://127.0.0.1:2323/m3u?token=test-token"


def test_m3u8_ad_filter_leaves_non_m3u8_url_unchanged() -> None:
    ad_filter = M3U8AdFilter()

    assert ad_filter.should_prepare("https://media.example/video.mp4") is False


def test_local_hls_proxy_server_returns_404_for_missing_token() -> None:
    server = LocalHlsProxyServer()

    status, headers, body = server.handle_request("GET", "/m3u?token=missing")

    assert status == 404
    assert headers == []
    assert body == b"missing proxy session"


def test_local_hls_proxy_server_returns_502_when_playlist_fetch_fails() -> None:
    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool):
        raise RuntimeError("origin down")

    server = LocalHlsProxyServer(get=fake_get)
    playlist_url = server.create_playlist_url("https://media.example/path/index.m3u8", {})
    token = playlist_url.rsplit("=", 1)[-1]

    status, headers, body = server.handle_request("GET", f"/m3u?token={token}")

    assert status == 502
    assert headers == []
    assert body == b"origin down"
