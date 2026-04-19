from pathlib import Path

from atv_player.player.m3u8_ad_filter import M3U8AdFilter, rewrite_media_playlist


def test_rewrite_media_playlist_removes_explicit_adjumps_and_redundant_discontinuities() -> None:
    playlist = """#EXTM3U
#EXTINF:4.170833,
0000073.ts
#EXTINF:5.171833,
0000074.ts
#EXT-X-DISCONTINUITY
#EXTINF:3,
/video/adjump/time/17739416073640000000.ts
#EXTINF:2,
/video/adjump/time/17739416073640000001.ts
#EXT-X-DISCONTINUITY
#EXTINF:1.042711,
0000075.ts
"""

    result = rewrite_media_playlist(
        playlist,
        "https://media.example/path/index.m3u8",
    )

    assert result.changed is True
    assert "/video/adjump/" not in result.text
    assert result.text.count("#EXT-X-DISCONTINUITY") == 0
    assert "https://media.example/path/0000073.ts" in result.text
    assert "https://media.example/path/0000074.ts" in result.text
    assert "https://media.example/path/0000075.ts" in result.text


def test_rewrite_media_playlist_removes_explicit_cue_out_blocks() -> None:
    playlist = """#EXTM3U
#EXTINF:5.0,
main-0001.ts
#EXT-X-CUE-OUT:30
#EXTINF:10.0,
ad-0001.ts
#EXTINF:10.0,
ad-0002.ts
#EXT-X-CUE-IN
#EXTINF:5.0,
main-0002.ts
"""

    result = rewrite_media_playlist(
        playlist,
        "https://media.example/path/index.m3u8",
    )

    assert result.changed is True
    assert "#EXT-X-CUE-OUT:30" not in result.text
    assert "#EXT-X-CUE-IN" not in result.text
    assert "https://media.example/path/ad-0001.ts" not in result.text
    assert "https://media.example/path/ad-0002.ts" not in result.text
    assert "https://media.example/path/main-0001.ts" in result.text
    assert "https://media.example/path/main-0002.ts" in result.text


def test_rewrite_media_playlist_removes_explicit_scte35_out_in_blocks() -> None:
    playlist = """#EXTM3U
#EXTINF:6.0,
main-0001.ts
#EXT-X-SCTE35-OUT
#EXTINF:15.0,
ad-0001.ts
#EXTINF:15.0,
ad-0002.ts
#EXT-X-SCTE35-IN
#EXTINF:6.0,
main-0002.ts
"""

    result = rewrite_media_playlist(
        playlist,
        "https://media.example/path/index.m3u8",
    )

    assert result.changed is True
    assert "#EXT-X-SCTE35-OUT" not in result.text
    assert "#EXT-X-SCTE35-IN" not in result.text
    assert "https://media.example/path/ad-0001.ts" not in result.text
    assert "https://media.example/path/ad-0002.ts" not in result.text
    assert "https://media.example/path/main-0001.ts" in result.text
    assert "https://media.example/path/main-0002.ts" in result.text


def test_rewrite_media_playlist_removes_scte35_daterange_blocks_by_duration() -> None:
    playlist = """#EXTM3U
#EXTINF:6.0,
main-0001.ts
#EXT-X-DATERANGE:ID="ad-1",CLASS="com.apple.scte35",START-DATE="2026-04-19T12:00:00Z",DURATION=20
#EXTINF:10.0,
ad-0001.ts
#EXTINF:10.0,
ad-0002.ts
#EXTINF:6.0,
main-0002.ts
"""

    result = rewrite_media_playlist(
        playlist,
        "https://media.example/path/index.m3u8",
    )

    assert result.changed is True
    assert "com.apple.scte35" not in result.text
    assert "https://media.example/path/ad-0001.ts" not in result.text
    assert "https://media.example/path/ad-0002.ts" not in result.text
    assert "https://media.example/path/main-0001.ts" in result.text
    assert "https://media.example/path/main-0002.ts" in result.text


def test_rewrite_media_playlist_removes_high_confidence_discontinuity_ad_block() -> None:
    playlist = """#EXTM3U
#EXTINF:6.0,
https://media.example/main-0001.ts
#EXT-X-DISCONTINUITY
#EXTINF:15.0,
https://ads.example.com/commercial/ad-0001.ts
#EXTINF:15.0,
https://ads.example.com/commercial/ad-0002.ts
#EXT-X-DISCONTINUITY
#EXTINF:6.0,
https://media.example/main-0002.ts
"""

    result = rewrite_media_playlist(
        playlist,
        "https://media.example/path/index.m3u8",
    )

    assert result.changed is True
    assert "ads.example.com" not in result.text
    assert "/commercial/" not in result.text
    assert result.text.count("#EXT-X-DISCONTINUITY") == 0
    assert "https://media.example/main-0001.ts" in result.text
    assert "https://media.example/main-0002.ts" in result.text


def test_rewrite_media_playlist_keeps_non_ad_discontinuity_blocks_without_enough_signals() -> None:
    playlist = """#EXTM3U
#EXTINF:6.0,
https://media.example/main-0001.ts
#EXT-X-DISCONTINUITY
#EXTINF:15.0,
https://backup.example.com/segment-0001.ts
#EXTINF:15.0,
https://backup.example.com/segment-0002.ts
#EXT-X-DISCONTINUITY
#EXTINF:6.0,
https://media.example/main-0002.ts
"""

    result = rewrite_media_playlist(
        playlist,
        "https://media.example/path/index.m3u8",
    )

    assert result.changed is False
    assert "backup.example.com" in result.text
    assert result.text.count("#EXT-X-DISCONTINUITY") == 2


def test_m3u8_ad_filter_writes_cleaned_playlist_to_cache(tmp_path: Path) -> None:
    requests: list[tuple[str, dict[str, str], float, bool]] = []

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool) -> FakeResponse:
        requests.append((url, headers, timeout, follow_redirects))
        return FakeResponse(
            """#EXTM3U
#EXTINF:4.0,
0001.ts
#EXTINF:2.0,
/video/adjump/time/0002.ts
#EXTINF:4.0,
0003.ts
"""
        )

    ad_filter = M3U8AdFilter(cache_dir=tmp_path, get=fake_get)

    prepared = ad_filter.prepare(
        "https://media.example/path/index.m3u8",
        {"Referer": "https://site.example"},
    )

    prepared_path = Path(prepared)
    assert prepared_path.exists() is True
    assert prepared_path.suffix == ".m3u8"
    assert "/video/adjump/" not in prepared_path.read_text(encoding="utf-8")
    assert requests == [
        (
            "https://media.example/path/index.m3u8",
            {"Referer": "https://site.example"},
            10.0,
            True,
        )
    ]


def test_m3u8_ad_filter_returns_original_url_when_fetch_fails(tmp_path: Path) -> None:
    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool):
        raise RuntimeError("network down")

    ad_filter = M3U8AdFilter(cache_dir=tmp_path, get=fake_get)

    original = "https://media.example/path/index.m3u8"
    prepared = ad_filter.prepare(original, {"Referer": "https://site.example"})

    assert prepared == original


def test_m3u8_ad_filter_returns_original_url_for_master_playlist(tmp_path: Path) -> None:
    class FakeResponse:
        text = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1280000
sub/playlist.m3u8
"""

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool) -> FakeResponse:
        return FakeResponse()

    ad_filter = M3U8AdFilter(cache_dir=tmp_path, get=fake_get)

    original = "https://media.example/master.m3u8"
    prepared = ad_filter.prepare(original)

    assert prepared == original


def test_m3u8_ad_filter_recurses_from_master_playlist_into_media_playlist(tmp_path: Path) -> None:
    requests: list[str] = []

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool) -> FakeResponse:
        requests.append(url)
        if url == "https://media.example/master.m3u8":
            return FakeResponse(
                """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1280000
sub/playlist.m3u8
"""
            )
        if url == "https://media.example/sub/playlist.m3u8":
            return FakeResponse(
                """#EXTM3U
#EXTINF:4.0,
main-0001.ts
#EXTINF:2.0,
/video/adjump/time/0002.ts
#EXTINF:4.0,
main-0003.ts
"""
            )
        raise AssertionError(f"unexpected url: {url}")

    ad_filter = M3U8AdFilter(cache_dir=tmp_path, get=fake_get)

    prepared = ad_filter.prepare("https://media.example/master.m3u8")

    prepared_path = Path(prepared)
    assert prepared_path.exists() is True
    assert "/video/adjump/" not in prepared_path.read_text(encoding="utf-8")
    assert requests == [
        "https://media.example/master.m3u8",
        "https://media.example/sub/playlist.m3u8",
    ]
