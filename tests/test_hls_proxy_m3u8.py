from atv_player.proxy.m3u8 import rewrite_playlist
from atv_player.proxy.session import ProxySessionRegistry


def test_rewrite_playlist_rewrites_media_segments_and_assets() -> None:
    registry = ProxySessionRegistry()
    token = registry.create_session(
        playlist_url="https://media.example/path/index.m3u8",
        headers={"Referer": "https://site.example"},
    )
    content = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="enc.key"
#EXT-X-MAP:URI="init.mp4"
#EXTINF:5.0,
main-0001.ts
#EXTINF:0.5,
ad-0002.ts
#EXTINF:5.0,
main-0003.ts
"""

    rewritten = rewrite_playlist(
        token=token,
        playlist_url="https://media.example/path/index.m3u8",
        content=content,
        session_registry=registry,
        proxy_base_url="http://127.0.0.1:2323",
    )

    assert 'URI="http://127.0.0.1:2323/asset?token=' in rewritten.text
    assert "http://127.0.0.1:2323/seg?token=" in rewritten.text
    assert "ad-0002.ts" not in rewritten.text
    session = registry.get(token)
    assert [segment.url for segment in session.segments] == [
        "https://media.example/path/main-0001.ts",
        "https://media.example/path/main-0003.ts",
    ]


def test_rewrite_playlist_rewrites_master_playlist_to_child_tokens() -> None:
    registry = ProxySessionRegistry()
    token = registry.create_session(
        playlist_url="https://media.example/master.m3u8",
        headers={},
    )
    content = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1280000
video/720.m3u8
"""

    rewritten = rewrite_playlist(
        token=token,
        playlist_url="https://media.example/master.m3u8",
        content=content,
        session_registry=registry,
        proxy_base_url="http://127.0.0.1:2323",
    )

    assert "http://127.0.0.1:2323/m3u?token=" in rewritten.text
    assert rewritten.is_master is True


def test_proxy_session_registry_expires_stale_sessions() -> None:
    registry = ProxySessionRegistry(ttl_seconds=5.0)
    token = registry.create_session("https://media.example/master.m3u8", {})

    registry.expire_stale(now=registry.get(token).created_at + 6.0)

    assert registry.contains(token) is False


def test_proxy_session_registry_expires_stale_sessions_when_creating_new_session(monkeypatch) -> None:
    registry = ProxySessionRegistry(ttl_seconds=5.0)
    stale_token = registry.create_session("https://media.example/old.m3u8", {})
    registry._sessions[stale_token].last_accessed_at = 10.0

    monkeypatch.setattr("atv_player.proxy.session.time.time", lambda: 16.0)

    fresh_token = registry.create_session("https://media.example/new.m3u8", {})

    assert stale_token not in registry._sessions
    assert fresh_token in registry._sessions


def test_rewrite_playlist_keeps_non_ad_discontinuity_blocks_stable() -> None:
    registry = ProxySessionRegistry()
    token = registry.create_session("https://media.example/path/index.m3u8", {})
    content = """#EXTM3U
#EXTINF:5.0,
main-0001.ts
#EXT-X-DISCONTINUITY
#EXTINF:5.0,
main-0002.ts
"""

    rewritten = rewrite_playlist(
        token=token,
        playlist_url="https://media.example/path/index.m3u8",
        content=content,
        session_registry=registry,
        proxy_base_url="http://127.0.0.1:2323",
    )

    assert rewritten.text.count("#EXT-X-DISCONTINUITY") == 1


def test_rewrite_playlist_keeps_short_live_segments_without_ad_markers() -> None:
    registry = ProxySessionRegistry()
    token = registry.create_session("https://media.example/path/index.m3u8", {})
    content = """#EXTM3U
#EXTINF:0.5,
live-0001.ts
#EXTINF:0.5,
live-0002.ts
"""

    rewritten = rewrite_playlist(
        token=token,
        playlist_url="https://media.example/path/index.m3u8",
        content=content,
        session_registry=registry,
        proxy_base_url="http://127.0.0.1:2323",
    )

    assert rewritten.text.count("http://127.0.0.1:2323/seg?token=") == 2
    session = registry.get(token)
    assert [segment.url for segment in session.segments] == [
        "https://media.example/path/live-0001.ts",
        "https://media.example/path/live-0002.ts",
    ]
