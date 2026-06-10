from __future__ import annotations

import base64
import json
import subprocess
from types import SimpleNamespace

import pytest

from atv_player.models import AppConfig, PlayItem, VodItem, YtdlpAudioTrackOption
from atv_player.network_proxy import ProxyConfig, ProxyDecider


@pytest.fixture(autouse=True)
def stub_system_ytdlp(monkeypatch):
    monkeypatch.setattr("atv_player.yt_dlp_service.resolve_system_ytdlp_path", lambda: "/usr/bin/yt-dlp")


@pytest.fixture
def service():
    from atv_player.yt_dlp_service import YtdlpPlaybackService
    svc = YtdlpPlaybackService()
    return svc


def _sample_info(**overrides):
    info = {
        "id": "test123",
        "title": "Test Video",
        "thumbnail": "https://img.test/thumb.jpg",
        "description": "A test video description",
        "duration": 300,
        "duration_string": "5:00",
        "extractor": "youtube",
        "channel": "OpenAI",
        "uploader": "OpenAI",
        "upload_date": "20260520",
        "view_count": 1234567,
        "like_count": 54321,
        "comment_count": 987,
        "url": "https://stream.test/direct.mp4",
        "http_headers": {"Referer": "https://www.youtube.com/", "User-Agent": "test"},
        "formats": [
            {
                "format_id": "1080",
                "url": "https://stream.test/1080.mp4",
                "height": 1080,
                "width": 1920,
                "tbr": 5000,
                "vcodec": "avc1",
                "acodec": "mp4a",
            },
            {
                "format_id": "720",
                "url": "https://stream.test/720.mp4",
                "height": 720,
                "width": 1280,
                "tbr": 2500,
                "vcodec": "avc1",
                "acodec": "mp4a",
            },
            {
                "format_id": "360",
                "url": "https://stream.test/360.mp4",
                "height": 360,
                "width": 640,
                "tbr": 800,
                "vcodec": "avc1",
                "acodec": "mp4a",
            },
            {
                "format_id": "audio",
                "url": "https://stream.test/audio.mp4",
                "height": None,
                "vcodec": "none",
                "acodec": "mp4a",
            },
        ],
        "subtitles": {
            "en": [{"url": "https://sub.test/en.srt", "ext": "srt"}],
            "zh-Hans": [{"url": "https://sub.test/zh.srt", "ext": "srt"}],
        },
        "automatic_captions": {
            "ja": [{"url": "https://sub.test/ja_auto.vtt", "ext": "vtt"}],
        },
    }
    info.update(overrides)
    return info


def _stub_extract_info(monkeypatch, service, payload):
    calls: list[tuple[str, int | None, bool]] = []

    def fake_extract_info(
        url: str,
        max_height: int | None,
        *,
        include_subtitles: bool = True,
        use_format_selector: bool = True,
    ):
        del use_format_selector
        calls.append((url, max_height, include_subtitles))
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(service, "_extract_info_via_command", fake_extract_info)
    return calls


class TestIsAvailable:
    def test_not_installed(self, service):
        service._ytdlp_path = None
        assert service.is_available() is True

    def test_returns_false_when_system_ytdlp_missing(self, monkeypatch, service):
        service._ytdlp_path = None
        monkeypatch.setattr("atv_player.yt_dlp_service.resolve_system_ytdlp_path", lambda: "")
        assert service.is_available() is False

    def test_extract_info_via_command_includes_browser_cookies(self, monkeypatch, service):
        monkeypatch.setenv("ATV_YTDLP_COOKIES_FROM_BROWSER", "chrome")
        run_calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            run_calls.append(command)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(_sample_info()),
                stderr="",
            )

        monkeypatch.setattr("atv_player.yt_dlp_service.subprocess.run", fake_run)

        result = service._extract_info_via_command("https://www.youtube.com/watch?v=test123", 1080)

        assert result["id"] == "test123"
        command = run_calls[0]
        assert "--cookies-from-browser" in command
        assert command[command.index("--cookies-from-browser") + 1] == "chrome"

    def test_extract_info_via_command_prefers_configured_cookie_browser(self, monkeypatch) -> None:
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        run_calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            run_calls.append(command)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(_sample_info()),
                stderr="",
            )

        monkeypatch.setattr("atv_player.yt_dlp_service.subprocess.run", fake_run)
        service = YtdlpPlaybackService(
            config_loader=lambda: AppConfig(youtube_cookie_browser="firefox")
        )

        service._extract_info_via_command("https://www.youtube.com/watch?v=test123", 1080)

        command = run_calls[0]
        assert "--cookies-from-browser" in command
        assert command[command.index("--cookies-from-browser") + 1] == "firefox"

    def test_extract_flat_playlist_uses_browser_cookies_and_playlist_window(self, monkeypatch) -> None:
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        run_calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            run_calls.append(command)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "entries": [
                        {"id": "abc123", "title": "订阅视频"},
                    ],
                }),
                stderr="",
            )

        monkeypatch.setattr("atv_player.yt_dlp_service.subprocess.run", fake_run)
        service = YtdlpPlaybackService(
            config_loader=lambda: AppConfig(youtube_cookie_browser="edge")
        )

        entries = service.extract_flat_playlist(":ytsubs", page=2, page_size=30)

        assert entries == [{"id": "abc123", "title": "订阅视频"}]
        command = run_calls[0]
        assert "--flat-playlist" in command
        assert command[command.index("--playlist-start") + 1] == "31"
        assert command[command.index("--playlist-end") + 1] == "60"
        assert command[command.index("--cookies-from-browser") + 1] == "edge"

    def test_extract_flat_playlist_preserves_playlist_count_total(self, monkeypatch) -> None:
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        def fake_run(command, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "playlist_count": 186,
                    "entries": [
                        {"id": "abc123", "title": "搜索结果"},
                    ],
                }),
                stderr="",
            )

        monkeypatch.setattr("atv_player.yt_dlp_service.subprocess.run", fake_run)
        service = YtdlpPlaybackService()

        entries = service.extract_flat_playlist(
            "ytsearchall:openai",
            page=1,
            page_size=30,
        )

        assert entries == [{"id": "abc123", "title": "搜索结果"}]
        assert entries.total == 186

    def test_extract_flat_playlist_returns_single_video_info_without_entries(self, monkeypatch) -> None:
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        def fake_run(command, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "id": "abc123",
                    "title": "真实 YouTube 标题",
                    "webpage_url": "https://www.youtube.com/watch?v=abc123",
                }),
                stderr="",
            )

        monkeypatch.setattr("atv_player.yt_dlp_service.subprocess.run", fake_run)
        service = YtdlpPlaybackService()

        entries = service.extract_flat_playlist("https://www.youtube.com/watch?v=abc123", page=1, page_size=1)

        assert entries == [
            {
                "id": "abc123",
                "title": "真实 YouTube 标题",
                "webpage_url": "https://www.youtube.com/watch?v=abc123",
            }
        ]

    def test_extract_info_via_command_includes_configured_language_and_region(self, monkeypatch) -> None:
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        run_calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            run_calls.append(command)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(_sample_info()),
                stderr="",
            )

        monkeypatch.setattr("atv_player.yt_dlp_service.subprocess.run", fake_run)
        service = YtdlpPlaybackService(
            config_loader=lambda: AppConfig(youtube_metadata_language="zh-CN", youtube_region="CN")
        )

        service._extract_info_via_command("https://www.youtube.com/watch?v=test123", 1080)

        command = run_calls[0]
        assert "--extractor-args" in command
        assert command[command.index("--extractor-args") + 1] == "youtube:lang=zh-CN"
        assert "--xff" in command
        assert command[command.index("--xff") + 1] == "CN"

    def test_extract_info_via_command_includes_proxy_when_manual_proxy_is_selected(self, monkeypatch, service):
        run_calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            run_calls.append(command)
            return SimpleNamespace(returncode=0, stdout=json.dumps(_sample_info()), stderr="")

        monkeypatch.setattr("atv_player.yt_dlp_service.subprocess.run", fake_run)
        service._proxy_decider = ProxyDecider(
            ProxyConfig(mode="socks5", proxy_url="socks5://127.0.0.1:1080", bypass_rules=[])
        )

        service._extract_info_via_command("https://www.youtube.com/watch?v=test123", 1080)

        command = run_calls[0]
        assert "--proxy" in command
        assert command[command.index("--proxy") + 1] == "socks5://127.0.0.1:1080"

    def test_extract_info_via_command_skips_proxy_for_bypass_target(self, monkeypatch, service):
        run_calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            run_calls.append(command)
            return SimpleNamespace(returncode=0, stdout=json.dumps(_sample_info()), stderr="")

        monkeypatch.setattr("atv_player.yt_dlp_service.subprocess.run", fake_run)
        service._proxy_decider = ProxyDecider(
            ProxyConfig(
                mode="socks5",
                proxy_url="socks5://127.0.0.1:1080",
                bypass_rules=["www.youtube.com"],
            )
        )

        service._extract_info_via_command("https://www.youtube.com/watch?v=test123", 1080)

        assert "--proxy" not in run_calls[0]


class TestCanResolve:
    def test_known_domain(self, service):
        assert service.can_resolve("https://www.youtube.com/watch?v=test") is True
        assert service.can_resolve("https://twitter.com/user/status/123") is True
        assert service.can_resolve("https://x.com/user/status/123") is True
        assert service.can_resolve("https://youtu.be/test123") is True
        assert service.can_resolve("yt:video:test123") is True
        assert service.can_resolve("abc123xyz89") is True

    def test_unknown_domain(self, service):
        assert service.can_resolve("https://example.com/video.mp4") is False
        assert service.can_resolve("https://random-site.org/watch/123") is False

    def test_empty_url(self, service):
        assert service.can_resolve("") is False
        assert service.can_resolve("  ") is False

    def test_not_available(self, monkeypatch, service):
        service._ytdlp_path = None
        monkeypatch.setattr("atv_player.yt_dlp_service.resolve_system_ytdlp_path", lambda: "")
        assert service.can_resolve("https://www.youtube.com/watch?v=test") is False


class TestResolve:
    def test_resolve_fast_uses_get_url_without_subtitle_or_json_enumeration(self, monkeypatch, service):
        run_calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            run_calls.append(command)
            return SimpleNamespace(
                returncode=0,
                stdout="https://video.test/1080.mp4\nhttps://audio.test/audio.webm\n",
                stderr="",
            )

        monkeypatch.setattr("atv_player.yt_dlp_service.subprocess.run", fake_run)

        result = service.resolve_fast("https://www.youtube.com/watch?v=test123")

        assert result.url == "https://video.test/1080.mp4"
        assert result.audio_url == "https://audio.test/audio.webm"
        assert result.selected_quality_id == "ytdlp_1080"
        command = run_calls[0]
        assert "--get-url" in command
        assert "--dump-single-json" not in command
        assert "--all-subs" not in command

    def test_resolve_fast_falls_back_when_get_url_returns_video_only_stream(self, monkeypatch, service):
        run_calls: list[list[str]] = []
        video_only_url = "https://rr4---sn-i3b7kn6k.googlevideo.com/videoplayback?expire=4102444800&itag=399"

        def fake_run(command, **_kwargs):
            run_calls.append(command)
            if "--get-url" in command:
                return SimpleNamespace(returncode=0, stdout=f"{video_only_url}\n", stderr="")
            return SimpleNamespace(returncode=0, stdout=json.dumps(_sample_info()), stderr="")

        monkeypatch.setattr("atv_player.yt_dlp_service.subprocess.run", fake_run)

        result = service.resolve_fast("https://www.youtube.com/watch?v=test123")

        assert result.url == "https://stream.test/1080.mp4"
        assert result.selected_quality_id == "ytdlp_1080"
        assert any("--get-url" in command for command in run_calls)
        assert any("--dump-single-json" in command for command in run_calls)

    def test_fast_format_selector_prefers_m3u_for_1080_and_vp9_for_2k(self) -> None:
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        service = YtdlpPlaybackService()

        command_1080 = service._extract_fast_urls_command("https://www.youtube.com/watch?v=test123", 1080)
        selector_1080 = command_1080[command_1080.index("--format") + 1]
        assert selector_1080.startswith("best[height<=1080]/bestvideo[height<=1080]+bestaudio")

        command_2160 = service._extract_fast_urls_command("https://www.youtube.com/watch?v=test123", 2160)
        selector_2160 = command_2160[command_2160.index("--format") + 1]
        assert selector_2160.startswith("bestvideo[height<=2160][vcodec^=vp9]+bestaudio")

    def test_fast_format_selector_can_prefer_av1_for_2k(self) -> None:
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        service = YtdlpPlaybackService(config_loader=lambda: AppConfig(youtube_video_codec="av1"))

        command = service._extract_fast_urls_command("https://www.youtube.com/watch?v=test123", 2160)
        selector = command[command.index("--format") + 1]

        assert selector.startswith("bestvideo[height<=2160][vcodec^=av01]+bestaudio")

    def test_prefers_original_english_audio_track(self, monkeypatch, service):
        info = _sample_info(
            formats=[
                {
                    "format_id": "137",
                    "url": "https://stream.test/video-1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 5000,
                    "vcodec": "avc1",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "140-zh",
                    "url": "https://stream.test/audio-zh.m4a",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a",
                    "ext": "m4a",
                    "language": "zh",
                    "format_note": "dubbed",
                },
                {
                    "format_id": "140-en",
                    "url": "https://stream.test/audio-en.m4a",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a",
                    "ext": "m4a",
                    "language": "en",
                    "format_note": "original",
                    "language_preference": 10,
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert [track.id for track in result.audio_tracks] == ["ytdlp_audio_en_140-en", "ytdlp_audio_zh_140-zh"]
        assert result.selected_audio_track_id == "ytdlp_audio_en_140-en"
        assert result.audio_format_id == "140-en"

    def test_prefers_configured_chinese_audio_track(self, monkeypatch) -> None:
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        service = YtdlpPlaybackService(config_loader=lambda: AppConfig(youtube_default_audio_lang="zh"))
        info = _sample_info(
            formats=[
                {
                    "format_id": "137",
                    "url": "https://stream.test/video-1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 5000,
                    "vcodec": "avc1",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "140-zh",
                    "url": "https://stream.test/audio-zh.m4a",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a",
                    "ext": "m4a",
                    "language": "zh",
                    "format_note": "dubbed",
                },
                {
                    "format_id": "140-en",
                    "url": "https://stream.test/audio-en.m4a",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a",
                    "ext": "m4a",
                    "language": "en",
                    "format_note": "original",
                    "language_preference": 10,
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert result.selected_audio_track_id == "ytdlp_audio_zh_140-zh"
        assert result.audio_format_id == "140-zh"

    def test_prefers_configured_chinese_audio_track_when_youtube_returns_zh_hans(self, monkeypatch) -> None:
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        service = YtdlpPlaybackService(config_loader=lambda: AppConfig(youtube_default_audio_lang="zh"))
        info = _sample_info(
            url="https://stream.test/master.m3u8",
            formats=[
                {
                    "format_id": "96-22",
                    "url": "https://stream.test/hls-1080-en.m3u8",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 4717,
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                    "language": "en",
                    "format_note": "English original (default)",
                    "language_preference": 10,
                },
                {
                    "format_id": "96-11",
                    "url": "https://stream.test/hls-1080-zh.m3u8",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 4717,
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                    "language": "zh-Hans",
                    "format_note": "Chinese (Simplified)",
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert result.selected_audio_track_id == "ytdlp_audio_zh-Hans_muxed"
        assert result.url == "https://stream.test/hls-1080-zh.m3u8"
        assert result.video_format_id == "96-11"

    def test_prefers_multilingual_youtube_muxed_tracks_over_split_audio_only_tracks(self, monkeypatch, service):
        info = _sample_info(
            url="https://stream.test/master.m3u8",
            formats=[
                {
                    "format_id": "399",
                    "url": "https://stream.test/video-1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 1760,
                    "vcodec": "av01.0.08M.08",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio-en.webm",
                    "tbr": 127,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                    "language": "en",
                    "format_note": "English original (default), medium",
                    "language_preference": 10,
                },
                {
                    "format_id": "95-11",
                    "url": "https://stream.test/hls-720-zh.m3u8",
                    "height": 720,
                    "width": 1280,
                    "tbr": 2565,
                    "vcodec": "avc1.4D401F",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                    "language": "zh-Hans",
                    "format_note": "Chinese (Simplified)",
                },
                {
                    "format_id": "95-22",
                    "url": "https://stream.test/hls-720-en.m3u8",
                    "height": 720,
                    "width": 1280,
                    "tbr": 2565,
                    "vcodec": "avc1.4D401F",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                    "language": "en",
                    "format_note": "English original (default)",
                    "language_preference": 10,
                },
                {
                    "format_id": "96-11",
                    "url": "https://stream.test/hls-1080-zh.m3u8",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 4717,
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                    "language": "zh-Hans",
                    "format_note": "Chinese (Simplified)",
                },
                {
                    "format_id": "96-22",
                    "url": "https://stream.test/hls-1080-en.m3u8",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 4717,
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                    "language": "en",
                    "format_note": "English original (default)",
                    "language_preference": 10,
                },
            ],
            requested_formats=[
                {
                    "format_id": "399",
                    "url": "https://stream.test/video-1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 1760,
                    "vcodec": "av01.0.08M.08",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio-en.webm",
                    "tbr": 127,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                    "language": "en",
                    "format_note": "English original (default), medium",
                    "language_preference": 10,
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert [track.id for track in result.audio_tracks] == [
            "ytdlp_audio_en_muxed",
            "ytdlp_audio_zh-Hans_muxed",
        ]
        assert result.selected_audio_track_id == "ytdlp_audio_en_muxed"
        assert result.url == "https://stream.test/hls-1080-en.m3u8"
        assert result.audio_url == ""
        assert result.video_format_id == "96-22"
        assert result.audio_format_id == ""

    def test_configured_muxed_audio_language_does_not_exceed_max_height(self, monkeypatch) -> None:
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        service = YtdlpPlaybackService(config_loader=lambda: AppConfig(youtube_default_audio_lang="hi"))
        info = _sample_info(
            url="https://stream.test/master.m3u8",
            formats=[
                {
                    "format_id": "399",
                    "url": "https://stream.test/video-1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 1760,
                    "vcodec": "av01.0.08M.08",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio.webm",
                    "tbr": 127,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                },
                {
                    "format_id": "301-en",
                    "url": "https://stream.test/hls-1080-en.m3u8",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 4717,
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                    "language": "en",
                    "format_note": "English original",
                    "language_preference": 10,
                },
                {
                    "format_id": "301-hi",
                    "url": "https://stream.test/hls-4320-hi.m3u8",
                    "height": 4320,
                    "width": 7680,
                    "tbr": 49000,
                    "vcodec": "av01.0.17M.10",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                    "language": "hi",
                    "format_note": "Hindi",
                },
            ],
            requested_formats=[
                {
                    "format_id": "399",
                    "url": "https://stream.test/video-1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 1760,
                    "vcodec": "av01.0.08M.08",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio.webm",
                    "tbr": 127,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve(
            "https://www.youtube.com/watch?v=test123",
            max_height=1080,
            selected_audio_track_id="ytdlp_audio_hi_muxed",
        )

        assert result.video_format_id == "301-en"
        assert result.url == "https://stream.test/hls-1080-en.m3u8"
        assert result.selected_quality_id == "ytdlp_1080"

    def test_youtube_1080_prefers_m3u_even_when_codec_preference_is_av1(self, monkeypatch) -> None:
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        service = YtdlpPlaybackService(config_loader=lambda: AppConfig(youtube_video_codec="av1"))
        info = _sample_info(
            extractor="youtube",
            url="https://stream.test/master.m3u8",
            formats=[
                {
                    "format_id": "301",
                    "url": "https://stream.test/hls-1080.m3u8",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 8559,
                    "vcodec": "avc1.64002A",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                },
                {
                    "format_id": "335",
                    "url": "https://stream.test/video-1080-vp9.webm",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 6803,
                    "vcodec": "vp9.2",
                    "acodec": "none",
                    "ext": "webm",
                },
                {
                    "format_id": "699",
                    "url": "https://stream.test/video-1080-av1.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 5062,
                    "vcodec": "av01.0.09M.10",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio.webm",
                    "tbr": 146,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123", max_height=1080)

        assert result.video_format_id == "301"
        assert result.url == "https://stream.test/hls-1080.m3u8"

    def test_youtube_2k_and_above_default_prefers_vp9_codec(self, monkeypatch) -> None:
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        service = YtdlpPlaybackService()
        info = _sample_info(
            extractor="youtube",
            url="https://stream.test/master.m3u8",
            formats=[
                {
                    "format_id": "337",
                    "url": "https://stream.test/video-2160-vp9.webm",
                    "height": 2160,
                    "width": 3840,
                    "tbr": 26709,
                    "vcodec": "vp9.2",
                    "acodec": "none",
                    "ext": "webm",
                },
                {
                    "format_id": "701",
                    "url": "https://stream.test/video-2160-av1.mp4",
                    "height": 2160,
                    "width": 3840,
                    "tbr": 24569,
                    "vcodec": "av01.0.13M.10",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio.webm",
                    "tbr": 146,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123", max_height=2160)

        assert result.video_format_id == "337"
        manifest = base64.b64decode(result.url.partition(",")[2]).decode("utf-8")
        assert "https://stream.test/video-2160-vp9.webm" in manifest
        assert "https://stream.test/video-2160-av1.mp4" not in manifest

    def test_youtube_2k_and_above_can_prefer_av1_codec(self, monkeypatch) -> None:
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        service = YtdlpPlaybackService(config_loader=lambda: AppConfig(youtube_video_codec="av1"))
        info = _sample_info(
            extractor="youtube",
            url="https://stream.test/master.m3u8",
            formats=[
                {
                    "format_id": "337",
                    "url": "https://stream.test/video-2160-vp9.webm",
                    "height": 2160,
                    "width": 3840,
                    "tbr": 26709,
                    "vcodec": "vp9.2",
                    "acodec": "none",
                    "ext": "webm",
                },
                {
                    "format_id": "701",
                    "url": "https://stream.test/video-2160-av1.mp4",
                    "height": 2160,
                    "width": 3840,
                    "tbr": 24569,
                    "vcodec": "av01.0.13M.10",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio.webm",
                    "tbr": 146,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123", max_height=2160)

        assert result.video_format_id == "701"
        manifest = base64.b64decode(result.url.partition(",")[2]).decode("utf-8")
        assert "https://stream.test/video-2160-av1.mp4" in manifest
        assert "https://stream.test/video-2160-vp9.webm" not in manifest

    def test_resolve_for_quality_preserves_selected_muxed_audio_language(self, monkeypatch, service):
        info = _sample_info(
            url="https://stream.test/master.m3u8",
            formats=[
                {
                    "format_id": "399",
                    "url": "https://stream.test/video-1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 1760,
                    "vcodec": "av01.0.08M.08",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio-en.webm",
                    "tbr": 127,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                    "language": "en",
                    "format_note": "English original (default), medium",
                    "language_preference": 10,
                },
                {
                    "format_id": "95-11",
                    "url": "https://stream.test/hls-720-zh.m3u8",
                    "height": 720,
                    "width": 1280,
                    "tbr": 2565,
                    "vcodec": "avc1.4D401F",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                    "language": "zh-Hans",
                    "format_note": "Chinese (Simplified)",
                },
                {
                    "format_id": "95-22",
                    "url": "https://stream.test/hls-720-en.m3u8",
                    "height": 720,
                    "width": 1280,
                    "tbr": 2565,
                    "vcodec": "avc1.4D401F",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                    "language": "en",
                    "format_note": "English original (default)",
                    "language_preference": 10,
                },
                {
                    "format_id": "96-11",
                    "url": "https://stream.test/hls-1080-zh.m3u8",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 4717,
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                    "language": "zh-Hans",
                    "format_note": "Chinese (Simplified)",
                },
                {
                    "format_id": "96-22",
                    "url": "https://stream.test/hls-1080-en.m3u8",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 4717,
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                    "language": "en",
                    "format_note": "English original (default)",
                    "language_preference": 10,
                },
            ],
            requested_formats=[
                {
                    "format_id": "399",
                    "url": "https://stream.test/video-1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 1760,
                    "vcodec": "av01.0.08M.08",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio-en.webm",
                    "tbr": 127,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                    "language": "en",
                    "format_note": "English original (default), medium",
                    "language_preference": 10,
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve_for_quality(
            "https://www.youtube.com/watch?v=test123",
            "ytdlp_720",
            audio_track_id="ytdlp_audio_zh-Hans_muxed",
        )

        assert result.selected_quality_id == "ytdlp_720"
        assert result.selected_audio_track_id == "ytdlp_audio_zh-Hans_muxed"
        assert result.url == "https://stream.test/hls-720-zh.m3u8"
        assert result.audio_url == ""
        assert result.video_format_id == "95-11"
        assert result.audio_format_id == ""

    def test_resolve_for_quality_retries_without_format_selector_when_requested_format_is_unavailable(
        self,
        monkeypatch,
        service,
    ) -> None:
        calls: list[tuple[int | None, bool, bool]] = []

        def fake_extract_info(
            url: str,
            max_height: int | None,
            *,
            include_subtitles: bool = True,
            use_format_selector: bool = True,
        ):
            del url
            calls.append((max_height, include_subtitles, use_format_selector))
            if use_format_selector:
                raise ValueError(
                    "下载错误: ERROR: [youtube] test123: Requested format is not available. "
                    "Use --list-formats for a list of available formats"
                )
            return _sample_info(
                url="https://stream.test/master.m3u8",
                formats=[
                    {
                        "format_id": "96",
                        "url": "https://stream.test/hls-1080.m3u8",
                        "height": 1080,
                        "width": 1920,
                        "tbr": 4717,
                        "vcodec": "avc1.640028",
                        "acodec": "mp4a.40.2",
                        "ext": "mp4",
                        "protocol": "m3u8_native",
                        "language": "en",
                    },
                ],
            )

        monkeypatch.setattr(service, "_extract_info_via_command", fake_extract_info)

        result = service.resolve_for_quality("https://www.youtube.com/watch?v=test123", "ytdlp_1080")

        assert calls == [(1080, False, True), (1080, False, False)]
        assert result.url == "https://stream.test/hls-1080.m3u8"
        assert result.video_format_id == "96"

    def test_resolve_for_quality_preserves_requested_audio_track(self, monkeypatch, service):
        info = _sample_info(
            formats=[
                {
                    "format_id": "137",
                    "url": "https://stream.test/video-1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 5000,
                    "vcodec": "avc1",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "136",
                    "url": "https://stream.test/video-720.mp4",
                    "height": 720,
                    "width": 1280,
                    "tbr": 2500,
                    "vcodec": "avc1",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "140-en",
                    "url": "https://stream.test/audio-en.m4a",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a",
                    "ext": "m4a",
                    "language": "en",
                    "format_note": "original",
                },
                {
                    "format_id": "140-zh",
                    "url": "https://stream.test/audio-zh.m4a",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a",
                    "ext": "m4a",
                    "language": "zh",
                    "format_note": "dubbed",
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve_for_quality(
            "https://www.youtube.com/watch?v=test123",
            "ytdlp_720",
            audio_track_id="ytdlp_audio_zh_140-zh",
        )

        assert result.selected_quality_id == "ytdlp_720"
        assert result.video_format_id == "136"
        assert result.selected_audio_track_id == "ytdlp_audio_zh_140-zh"
        assert result.audio_format_id == "140-zh"

    def test_prefers_same_height_muxed_youtube_stream_over_requested_split_pair(self, monkeypatch, service):
        info = _sample_info(
            url="https://stream.test/master.m3u8",
            formats=[
                {
                    "format_id": "301",
                    "url": "https://stream.test/master-1080.m3u8",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 5200,
                    "vcodec": "avc1.64002a",
                    "acodec": "mp4a.40.2",
                    "ext": "mp4",
                },
            ],
            requested_formats=[
                {
                    "format_id": "399",
                    "url": "https://stream.test/video-1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 5000,
                    "vcodec": "av01.0.09M.08",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio.webm",
                    "tbr": 126,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                    "audio_channels": 2,
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert result.url == "https://stream.test/master-1080.m3u8"
        assert result.audio_url == ""
        assert result.ytdl_format == ""
        assert result.video_format_id == "301"

    def test_prefers_requested_format_pair_over_local_codec_reselection(self, monkeypatch, service):
        info = _sample_info(
            url="https://stream.test/master.m3u8",
            requested_formats=[
                {
                    "format_id": "399",
                    "url": "https://stream.test/video-1080-av1.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 2600,
                    "vcodec": "av01.0.09M.08",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio-251.webm",
                    "tbr": 126,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                },
            ],
            formats=[
                {
                    "format_id": "299",
                    "url": "https://stream.test/video-1080-avc.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 4800,
                    "vcodec": "avc1.64002a",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "303",
                    "url": "https://stream.test/video-1080-vp9.webm",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 3200,
                    "vcodec": "vp9",
                    "acodec": "none",
                    "ext": "webm",
                },
                {
                    "format_id": "399",
                    "url": "https://stream.test/video-1080-av1.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 2600,
                    "vcodec": "av01.0.09M.08",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio-251.webm",
                    "tbr": 126,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                },
                {
                    "format_id": "140",
                    "url": "https://stream.test/audio-140.m4a",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "ext": "m4a",
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert result.url.startswith("data:application/dash+xml;base64,")
        assert result.audio_url == ""
        assert result.ytdl_format == ""
        manifest = base64.b64decode(result.url.partition(",")[2]).decode("utf-8")
        assert "https://stream.test/video-1080-av1.mp4" in manifest
        assert "https://stream.test/audio-251.webm" in manifest

    def test_embeds_segment_base_ranges_in_generated_dash_manifest(self, monkeypatch, service):
        info = _sample_info(
            extractor="vimeo",
            url="https://stream.test/master.m3u8",
            requested_formats=[
                {
                    "format_id": "299",
                    "url": "https://stream.test/video-1080-avc.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 4800,
                    "vcodec": "avc1.64002a",
                    "acodec": "none",
                    "ext": "mp4",
                    "init_range": {"start": "0", "end": "737"},
                    "index_range": {"start": "738", "end": "1425"},
                },
                {
                    "format_id": "140",
                    "url": "https://stream.test/audio-140.m4a",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "ext": "m4a",
                    "init_range": {"start": "0", "end": "701"},
                    "index_range": {"start": "702", "end": "1189"},
                },
            ],
            formats=[],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        manifest = base64.b64decode(result.url.partition(",")[2]).decode("utf-8")
        assert '<SegmentBase indexRange="738-1425"><Initialization range="0-737"/></SegmentBase>' in manifest
        assert '<SegmentBase indexRange="702-1189"><Initialization range="0-701"/></SegmentBase>' in manifest

    def test_escapes_ampersands_in_generated_dash_manifest_base_urls(self, monkeypatch, service):
        info = _sample_info(
            extractor="youtube",
            url="https://stream.test/master.m3u8",
            requested_formats=[
                {
                    "format_id": "299",
                    "url": "https://stream.test/video.mp4?expire=1&sig=abc",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 4800,
                    "vcodec": "avc1.64002a",
                    "acodec": "none",
                    "ext": "mp4",
                    "init_range": {"start": "0", "end": "737"},
                    "index_range": {"start": "738", "end": "1425"},
                },
                {
                    "format_id": "140",
                    "url": "https://stream.test/audio.m4a?expire=1&sig=def",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "ext": "m4a",
                    "init_range": {"start": "0", "end": "701"},
                    "index_range": {"start": "702", "end": "1189"},
                },
            ],
            formats=[],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        manifest = base64.b64decode(result.url.partition(",")[2]).decode("utf-8")
        assert "https://stream.test/video.mp4?expire=1&amp;sig=abc" in manifest
        assert "https://stream.test/audio.m4a?expire=1&amp;sig=def" in manifest

    def test_preserves_ytdlp_dash_http_chunk_size_in_generated_manifest(self, monkeypatch, service):
        info = _sample_info(
            extractor="youtube",
            url="https://stream.test/master.m3u8",
            requested_formats=[
                {
                    "format_id": "699",
                    "url": "https://stream.test/video-4k.mp4",
                    "height": 2160,
                    "width": 3840,
                    "tbr": 5000,
                    "vcodec": "av01.0.12M.10",
                    "acodec": "none",
                    "ext": "mp4",
                    "downloader_options": {"http_chunk_size": 10485760},
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio.webm",
                    "tbr": 145,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                    "downloader_options": {"http_chunk_size": 10485760},
                },
            ],
            formats=[],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        manifest = base64.b64decode(result.url.partition(",")[2]).decode("utf-8")
        assert (
            '<SupplementalProperty schemeIdUri="urn:atv-player:http-chunk-size" value="10485760"/>'
            in manifest
        )

    def test_uses_configured_default_startup_quality_when_resolve_does_not_specify_limit(self, monkeypatch):
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        info = _sample_info(
            extractor="youtube",
            requested_formats=[
                {
                    "format_id": "1080",
                    "url": "https://stream.test/1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 5000,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                }
            ],
        )
        service = YtdlpPlaybackService(
            config_loader=lambda: AppConfig(youtube_max_height=1080)
        )
        calls = _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert calls == [("https://www.youtube.com/watch?v=test123", None, False)]
        assert result.selected_quality_id == "ytdlp_1080"
        assert result.video_format_id == "1080"

    def test_prefers_configured_default_quality_when_available(self, monkeypatch):
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        info = _sample_info(
            extractor="youtube",
            requested_formats=[
                {
                    "format_id": "720",
                    "url": "https://stream.test/720.mp4",
                    "height": 720,
                    "width": 1280,
                    "tbr": 2500,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                }
            ],
        )
        service = YtdlpPlaybackService(
            config_loader=lambda: AppConfig(youtube_max_height=720)
        )
        calls = _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert calls == [("https://www.youtube.com/watch?v=test123", None, False)]
        assert result.selected_quality_id == "ytdlp_720"
        assert result.video_format_id == "720"

    def test_falls_back_to_highest_available_quality_when_configured_default_missing(self, monkeypatch):
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        info = _sample_info(
            extractor="youtube",
            requested_formats=[
                {
                    "format_id": "2160",
                    "url": "https://stream.test/2160.mp4",
                    "height": 2160,
                    "width": 3840,
                    "tbr": 12000,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                }
            ],
            formats=[
                {
                    "format_id": "2160",
                    "url": "https://stream.test/2160.mp4",
                    "height": 2160,
                    "width": 3840,
                    "tbr": 12000,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                },
                {
                    "format_id": "720",
                    "url": "https://stream.test/720.mp4",
                    "height": 720,
                    "width": 1280,
                    "tbr": 2500,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                },
            ],
        )
        service = YtdlpPlaybackService(
            config_loader=lambda: AppConfig(youtube_max_height=1080)
        )
        calls = _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert calls == [("https://www.youtube.com/watch?v=test123", None, False)]
        assert result.selected_quality_id == "ytdlp_2160"
        assert result.video_format_id == "2160"

    def test_ignores_unbounded_requested_formats_when_default_quality_is_available(self, monkeypatch):
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        info = _sample_info(
            extractor="youtube",
            requested_formats=[
                {
                    "format_id": "400",
                    "url": "https://stream.test/1440.mp4",
                    "height": 1440,
                    "width": 2560,
                    "tbr": 2200,
                    "vcodec": "av01.0.12M.08",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio.webm",
                    "tbr": 126,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                    "audio_channels": 2,
                },
            ],
            formats=[
                {
                    "format_id": "400",
                    "url": "https://stream.test/1440.mp4",
                    "height": 1440,
                    "width": 2560,
                    "tbr": 2200,
                    "vcodec": "av01.0.12M.08",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "399",
                    "url": "https://stream.test/1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 1000,
                    "vcodec": "av01.0.08M.08",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio.webm",
                    "tbr": 126,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                    "audio_channels": 2,
                },
            ],
        )
        service = YtdlpPlaybackService(
            config_loader=lambda: AppConfig(youtube_max_height=1080)
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert result.selected_quality_id == "ytdlp_1080"
        assert result.video_format_id == "399"
        manifest = base64.b64decode(result.url.partition(",")[2]).decode("utf-8")
        assert "https://stream.test/1080.mp4" in manifest
        assert "https://stream.test/1440.mp4" not in manifest

    def test_explicit_max_height_overrides_configured_default_max_height(self, monkeypatch):
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        info = _sample_info()
        service = YtdlpPlaybackService(
            config_loader=lambda: AppConfig(youtube_max_height=720)
        )
        calls = _stub_extract_info(monkeypatch, service, info)

        service.resolve("https://www.youtube.com/watch?v=test123", max_height=480)

        assert calls == [("https://www.youtube.com/watch?v=test123", 480, False)]

    def test_prefers_muxed_fallback_url_when_info_url_missing(self, monkeypatch, service):
        info = _sample_info(
            extractor="vimeo",
            url="",
            formats=[
                {
                    "format_id": "1080-video",
                    "url": "https://stream.test/1080-video.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 5000,
                    "vcodec": "avc1",
                    "acodec": "none",
                },
                {
                    "format_id": "720-muxed",
                    "url": "https://stream.test/720-muxed.mp4",
                    "height": 720,
                    "width": 1280,
                    "tbr": 2500,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert result.url == "https://stream.test/720-muxed.mp4"

    def test_uses_cached_result_before_ttl_expires(self, monkeypatch):
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        info = _sample_info()
        clock = {"now": 100.0}
        service = YtdlpPlaybackService(ttl_seconds=300.0, now=lambda: clock["now"])
        calls = _stub_extract_info(monkeypatch, service, info)

        first = service.resolve("https://www.youtube.com/watch?v=test123")
        second = service.resolve("https://www.youtube.com/watch?v=test123")

        assert first.url == "https://stream.test/1080.mp4"
        assert second.url == "https://stream.test/1080.mp4"
        assert len(calls) == 1

    def test_cache_key_includes_selected_audio_track(self, monkeypatch):
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        info = _sample_info(
            formats=[
                {
                    "format_id": "137",
                    "url": "https://stream.test/video-1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 5000,
                    "vcodec": "avc1",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "140-en",
                    "url": "https://stream.test/audio-en.m4a",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a",
                    "ext": "m4a",
                    "language": "en",
                    "format_note": "original",
                },
                {
                    "format_id": "140-zh",
                    "url": "https://stream.test/audio-zh.m4a",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a",
                    "ext": "m4a",
                    "language": "zh",
                    "format_note": "dubbed",
                },
            ],
        )
        clock = {"now": 100.0}
        service = YtdlpPlaybackService(ttl_seconds=300.0, now=lambda: clock["now"])
        calls = _stub_extract_info(monkeypatch, service, info)

        service.resolve("https://www.youtube.com/watch?v=test123", selected_audio_track_id="ytdlp_audio_en_140-en")
        service.resolve("https://www.youtube.com/watch?v=test123", selected_audio_track_id="ytdlp_audio_zh_140-zh")

        assert len(calls) == 2

    def test_cache_key_includes_youtube_preferences(self, monkeypatch):
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        config = AppConfig()
        info = _sample_info(
            formats=[
                {
                    "format_id": "137",
                    "url": "https://stream.test/video-1080.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 5000,
                    "vcodec": "avc1",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "140-en",
                    "url": "https://stream.test/audio-en.m4a",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a",
                    "ext": "m4a",
                    "language": "en",
                    "format_note": "original",
                },
                {
                    "format_id": "140-zh",
                    "url": "https://stream.test/audio-zh.m4a",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a",
                    "ext": "m4a",
                    "language": "zh",
                    "format_note": "dubbed",
                },
            ],
        )
        clock = {"now": 100.0}
        service = YtdlpPlaybackService(
            ttl_seconds=300.0,
            now=lambda: clock["now"],
            config_loader=lambda: config,
        )
        calls = _stub_extract_info(monkeypatch, service, info)

        first = service.resolve("https://www.youtube.com/watch?v=test123")
        config.youtube_default_audio_lang = "zh"
        config.youtube_default_subtitle_lang = "zh-CN"
        config.youtube_metadata_language = "zh-CN"
        config.youtube_region = "CN"
        second = service.resolve("https://www.youtube.com/watch?v=test123")

        assert first.selected_audio_track_id == "ytdlp_audio_en_140-en"
        assert second.selected_audio_track_id == "ytdlp_audio_zh_140-zh"
        assert len(calls) == 2

    def test_canonical_youtube_id_and_watch_url_share_cache(self, monkeypatch):
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        info = _sample_info()
        clock = {"now": 100.0}
        service = YtdlpPlaybackService(ttl_seconds=300.0, now=lambda: clock["now"])
        calls = _stub_extract_info(monkeypatch, service, info)

        first = service.resolve("yt:video:test123")
        second = service.resolve("https://www.youtube.com/watch?v=test123")

        assert second.url == first.url
        assert calls == [("https://www.youtube.com/watch?v=test123", None, False)]

    def test_bare_youtube_video_id_and_watch_url_share_cache(self, monkeypatch):
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        info = _sample_info(id="abc123xyz89")
        service = YtdlpPlaybackService(ttl_seconds=300.0)
        calls = _stub_extract_info(monkeypatch, service, info)

        first = service.resolve("abc123xyz89")
        second = service.resolve("https://www.youtube.com/watch?v=abc123xyz89")

        assert second.url == first.url
        assert calls == [("https://www.youtube.com/watch?v=abc123xyz89", None, False)]

    def test_quality_specific_resolve_reuses_unbounded_cache_when_selected_quality_matches(self, monkeypatch):
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        info = _sample_info(
            extractor="youtube",
            requested_formats=[
                {
                    "format_id": "337",
                    "url": "https://stream.test/video-2160.webm",
                    "height": 2160,
                    "width": 3840,
                    "tbr": 12000,
                    "vcodec": "vp09.00.51.08",
                    "acodec": "none",
                    "ext": "webm",
                    "init_range": {"start": "0", "end": "737"},
                    "index_range": {"start": "738", "end": "1425"},
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio-251.webm",
                    "tbr": 126,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                    "init_range": {"start": "0", "end": "701"},
                    "index_range": {"start": "702", "end": "1189"},
                },
            ],
            formats=[
                {
                    "format_id": "337",
                    "url": "https://stream.test/video-2160.webm",
                    "height": 2160,
                    "width": 3840,
                    "tbr": 12000,
                    "vcodec": "vp09.00.51.08",
                    "acodec": "none",
                    "ext": "webm",
                    "init_range": {"start": "0", "end": "737"},
                    "index_range": {"start": "738", "end": "1425"},
                },
                {
                    "format_id": "251",
                    "url": "https://stream.test/audio-251.webm",
                    "tbr": 126,
                    "vcodec": "none",
                    "acodec": "opus",
                    "ext": "webm",
                    "init_range": {"start": "0", "end": "701"},
                    "index_range": {"start": "702", "end": "1189"},
                },
            ],
        )
        clock = {"now": 100.0}
        service = YtdlpPlaybackService(ttl_seconds=300.0, now=lambda: clock["now"])
        calls = _stub_extract_info(monkeypatch, service, info)

        first = service.resolve("https://www.youtube.com/watch?v=test123")
        second = service.resolve_for_quality("https://www.youtube.com/watch?v=test123", "ytdlp_2160")

        assert first.selected_quality_id == "ytdlp_2160"
        assert second.selected_quality_id == "ytdlp_2160"
        assert second.url == first.url
        assert len(calls) == 1

    def test_re_extracts_after_cache_expiry(self, monkeypatch):
        from atv_player.yt_dlp_service import YtdlpPlaybackService

        info = _sample_info()
        clock = {"now": 100.0}
        service = YtdlpPlaybackService(ttl_seconds=5.0, now=lambda: clock["now"])
        calls = _stub_extract_info(monkeypatch, service, info)

        service.resolve("https://www.youtube.com/watch?v=test123")
        clock["now"] = 110.0
        service.resolve("https://www.youtube.com/watch?v=test123")

        assert len(calls) == 2

    def test_success(self, monkeypatch, service):
        info = _sample_info()
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert result.url == "https://stream.test/1080.mp4"
        assert result.audio_url == ""
        assert result.title == "Test Video"
        assert result.thumbnail == "https://img.test/thumb.jpg"
        assert result.description == "A test video description"
        assert result.duration_seconds == 300
        assert result.extractor == "youtube"
        assert result.headers == {"Referer": "https://www.youtube.com/", "User-Agent": "test"}
        assert result.selected_quality_id == "ytdlp_1080"
        assert result.ytdl_format == ""

    def test_prefers_selected_format_http_headers_when_top_level_headers_missing(self, monkeypatch, service):
        info = _sample_info(
            http_headers={},
            requested_formats=[
                {
                    "format_id": "299",
                    "url": "https://stream.test/video-1080-avc.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 4800,
                    "vcodec": "avc1.64002a",
                    "acodec": "none",
                    "ext": "mp4",
                    "http_headers": {
                        "Referer": "https://www.youtube.com/",
                        "User-Agent": "Mozilla/5.0 Test",
                    },
                },
                {
                    "format_id": "140",
                    "url": "https://stream.test/audio-140.m4a",
                    "tbr": 128,
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "ext": "m4a",
                    "http_headers": {
                        "Referer": "https://www.youtube.com/",
                        "User-Agent": "Mozilla/5.0 Test",
                    },
                },
            ],
            formats=[],
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert result.headers == {
            "Referer": "https://www.youtube.com/",
            "User-Agent": "Mozilla/5.0 Test",
        }

    def test_qualities(self, monkeypatch, service):
        info = _sample_info()
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert len(result.qualities) == 3
        assert result.qualities[0].height == 1080
        assert result.qualities[1].height == 720
        assert result.qualities[2].height == 360
        # audio-only format filtered out
        for q in result.qualities:
            assert q.height >= 360

    def test_subtitles(self, monkeypatch, service):
        info = _sample_info()
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert len(result.subtitles) == 2
        names = [s.name for s in result.subtitles]
        assert any("英文" in n for n in names)
        assert any("简体中文" in n for n in names)
        # Japanese is filtered out (only zh/en kept)

    def test_geo_restricted(self, monkeypatch, service):
        _stub_extract_info(monkeypatch, service, ValueError("该内容受地区限制"))

        with pytest.raises(ValueError, match="地区限制"):
            service.resolve("https://www.youtube.com/watch?v=test123")

    def test_extractor_error(self, monkeypatch, service):
        _stub_extract_info(monkeypatch, service, ValueError("下载错误: not found"))

        with pytest.raises(ValueError, match="下载错误"):
            service.resolve("https://www.youtube.com/watch?v=test123")

    def test_no_url(self, monkeypatch, service):
        info = _sample_info(url="", formats=[])
        _stub_extract_info(monkeypatch, service, info)

        with pytest.raises(ValueError, match="未获取到播放地址"):
            service.resolve("https://www.youtube.com/watch?v=test123")

    def test_no_result(self, monkeypatch, service):
        _stub_extract_info(monkeypatch, service, None)

        with pytest.raises(ValueError, match="未返回结果"):
            service.resolve("https://www.youtube.com/watch?v=test123")

    def test_startup_timeout_does_not_retry_subtitle_free_command(self, monkeypatch, service):
        run_calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            run_calls.append(command)
            raise subprocess.TimeoutExpired(command, timeout=60)

        monkeypatch.setattr("atv_player.yt_dlp_service.subprocess.run", fake_run)

        with pytest.raises(ValueError, match="yt-dlp 解析超时"):
            service.resolve("https://www.youtube.com/watch?v=test123")

        assert len(run_calls) == 1
        assert "--all-subs" not in run_calls[0]

    def test_startup_resolve_skips_subtitle_enumeration(self, monkeypatch, service):
        run_calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            run_calls.append(command)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(_sample_info(subtitles={}, automatic_captions={})),
                stderr="",
            )

        monkeypatch.setattr("atv_player.yt_dlp_service.subprocess.run", fake_run)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert result.url == "https://stream.test/1080.mp4"
        assert len(run_calls) == 1
        assert "--all-subs" not in run_calls[0]

    def test_not_available(self, monkeypatch, service):
        service._ytdlp_path = None
        monkeypatch.setattr("atv_player.yt_dlp_service.resolve_system_ytdlp_path", lambda: "")
        with pytest.raises(ValueError, match="未安装"):
            service.resolve("https://www.youtube.com/watch?v=test123")


class TestResolveToPlayItem:
    def test_prefers_current_resolved_height_over_highest_available_quality(self, monkeypatch, service):
        info = _sample_info(
            height=1080,
            formats=[
                {
                    "format_id": "2160-video",
                    "url": "https://stream.test/2160-video.mp4",
                    "height": 2160,
                    "width": 3840,
                    "tbr": 12000,
                    "vcodec": "vp9",
                    "acodec": "none",
                },
                {
                    "format_id": "1080-video",
                    "url": "https://stream.test/1080-video.mp4",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 5000,
                    "vcodec": "avc1",
                    "acodec": "none",
                },
                {
                    "format_id": "720-muxed",
                    "url": "https://stream.test/720-muxed.mp4",
                    "height": 720,
                    "width": 1280,
                    "tbr": 2500,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                },
            ],
        )
        _stub_extract_info(monkeypatch, service, info)

        _, item = service.resolve_to_play_item("https://www.youtube.com/watch?v=test123")

        assert [quality.id for quality in item.playback_qualities] == [
            "ytdlp_2160",
            "ytdlp_1080",
            "ytdlp_720",
        ]
        assert [quality.ytdl_format for quality in item.playback_qualities] == [
            "bestvideo[height<=2160][vcodec^=vp9]+bestaudio/"
            "bestvideo[height<=2160][vcodec^=vp09]+bestaudio/"
            "bestvideo[height<=2160]+bestaudio/best[height<=2160]/bestvideo+bestaudio/best",
            "best[height<=1080]/bestvideo[height<=1080]+bestaudio/bestvideo+bestaudio/best",
            "best[height<=720]/bestvideo[height<=720]+bestaudio/bestvideo+bestaudio/best",
        ]
        assert item.selected_playback_quality_id == "ytdlp_1080"

    def test_success(self, monkeypatch, service):
        info = _sample_info()
        _stub_extract_info(monkeypatch, service, info)

        vod, item = service.resolve_to_play_item("https://www.youtube.com/watch?v=test123")

        assert isinstance(vod, VodItem)
        assert isinstance(item, PlayItem)
        assert vod.vod_name == "Test Video"
        assert vod.vod_pic == "https://img.test/thumb.jpg"
        assert item.url == "https://stream.test/1080.mp4"
        assert item.original_url == "https://www.youtube.com/watch?v=test123"
        assert len(item.playback_qualities) == 3
        assert len(item.external_subtitles) == 2
        assert item.duration_seconds == 300
        assert item.selected_playback_quality_id == "ytdlp_1080"
        assert item.ytdl_format == ""

    def test_resolve_to_play_item_normalizes_youtube_id_source_url(self, monkeypatch, service):
        info = _sample_info()
        _stub_extract_info(monkeypatch, service, info)

        vod, item = service.resolve_to_play_item("yt:video:test123")

        assert vod.vod_id == "https://www.youtube.com/watch?v=test123"
        assert vod.vod_name == "Test Video"
        assert item.original_url == "https://www.youtube.com/watch?v=test123"
        assert item.vod_id == "https://www.youtube.com/watch?v=test123"

    def test_apply_result_overwrites_vod_and_play_item(self, monkeypatch, service):
        info = _sample_info(
            title="Resolved Title",
            thumbnail="https://img.test/resolved.jpg",
            description="",
            duration=321,
        )
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")
        vod = VodItem(
            vod_id="detail-1",
            vod_name="Original Title",
            vod_pic="https://img.test/original.jpg",
            vod_content="original description",
        )
        item = PlayItem(
            title="Original Episode",
            url="",
            original_url="",
            vod_id="detail-1",
            media_title="Original Media",
            duration_seconds=12,
            selected_playback_quality_id="",
        )

        service.apply_result(
            result,
            vod=vod,
            item=item,
            source_url="https://www.youtube.com/watch?v=test123",
        )

        assert vod.vod_id == "detail-1"
        assert vod.vod_name == "Resolved Title"
        assert vod.vod_pic == "https://img.test/resolved.jpg"
        assert vod.vod_content == ""
        assert item.url == "https://stream.test/1080.mp4"
        assert item.original_url == "https://www.youtube.com/watch?v=test123"
        assert item.title == "Resolved Title"
        assert item.media_title == "Resolved Title"
        assert item.duration_seconds == 321
        assert item.selected_playback_quality_id == "ytdlp_1080"
        assert len(item.playback_qualities) == 3
        assert len(item.external_subtitles) == 2
        assert [(field.label, field.value) for field in vod.detail_fields] == [
            ("频道", "OpenAI"),
            ("发布", "2026-05-20"),
            ("时长", "5:21"),
            ("播放", "123.5万"),
            ("点赞", "5.4万"),
            ("评论", "987"),
        ]
        assert [(field.label, field.value) for field in item.detail_fields] == [
            ("频道", "OpenAI"),
            ("发布", "2026-05-20"),
            ("时长", "5:21"),
            ("播放", "123.5万"),
            ("点赞", "5.4万"),
            ("评论", "987"),
        ]

    def test_apply_result_copies_ytdlp_audio_tracks_and_selected_audio_id(self, monkeypatch, service):
        from atv_player.yt_dlp_service import YtdlpResolveResult

        result = YtdlpResolveResult(
            url="https://stream.test/video.mp4",
            audio_url="https://stream.test/audio-en.m4a",
            ytdl_format="137+140",
            video_format_id="137",
            audio_format_id="140",
            audio_tracks=[
                YtdlpAudioTrackOption(
                    id="ytdlp_audio_en_140",
                    label="English Original",
                    lang="en",
                    format_id="140",
                    is_original=True,
                ),
                YtdlpAudioTrackOption(
                    id="ytdlp_audio_zh_140-dub",
                    label="中文配音",
                    lang="zh",
                    format_id="140-dub",
                ),
            ],
            selected_audio_track_id="ytdlp_audio_en_140",
            title="Test Video",
            thumbnail="https://img.test/thumb.jpg",
            description="A test video description",
            duration_seconds=300,
            headers={"Referer": "https://www.youtube.com/"},
            subtitles=[],
            qualities=[],
            selected_quality_id="",
            extractor="youtube",
            detail_fields=[],
        )
        item = PlayItem(title="Episode", url="", original_url="", vod_id="video-1")
        service.apply_result(result, item=item, source_url="https://www.youtube.com/watch?v=test123")

        assert [track.id for track in item.audio_tracks] == ["ytdlp_audio_en_140", "ytdlp_audio_zh_140-dub"]
        assert item.selected_audio_track_id == "ytdlp_audio_en_140"

    def test_apply_result_preserves_existing_chinese_title_when_configured_metadata_title_is_english(self):
        from atv_player.yt_dlp_service import YtdlpPlaybackService, YtdlpResolveResult

        service = YtdlpPlaybackService(config_loader=lambda: AppConfig(youtube_metadata_language="zh-CN"))
        result = YtdlpResolveResult(
            url="https://stream.test/video.mp4",
            audio_url="",
            ytdl_format="",
            video_format_id="",
            audio_format_id="",
            audio_tracks=[],
            selected_audio_track_id="",
            title="Last To Leave Grocery Store, Wins $250,000",
            thumbnail="",
            description="",
            duration_seconds=0,
            headers={},
            subtitles=[],
            qualities=[],
            selected_quality_id="",
            extractor="youtube",
            detail_fields=[],
        )
        vod = VodItem(vod_id="yt:video:zRtGL0-5rg4", vod_name="最后离开杂货店的人，赢得 25 万美元")
        item = PlayItem(
            title="最后离开杂货店的人，赢得 25 万美元",
            url="",
            original_url="yt:video:zRtGL0-5rg4",
            vod_id="yt:video:zRtGL0-5rg4",
        )

        service.apply_result(result, vod=vod, item=item, source_url="yt:video:zRtGL0-5rg4")

        assert vod.vod_name == "最后离开杂货店的人，赢得 25 万美元"
        assert item.title == "最后离开杂货店的人，赢得 25 万美元"
        assert item.media_title == "最后离开杂货店的人，赢得 25 万美元"

    def test_resolve_builds_ytdlp_detail_fields(self, monkeypatch, service):
        info = _sample_info()
        _stub_extract_info(monkeypatch, service, info)

        result = service.resolve("https://www.youtube.com/watch?v=test123")

        assert [(field.label, field.value) for field in result.detail_fields] == [
            ("频道", "OpenAI"),
            ("发布", "2026-05-20"),
            ("时长", "5:00"),
            ("播放", "123.5万"),
            ("点赞", "5.4万"),
            ("评论", "987"),
            ("简介", "A test video description"),
        ]


class TestBuildQualityOptions:
    def test_keeps_video_only_formats_for_quality_ladder(self):
        from atv_player.yt_dlp_service import _build_quality_options
        info = {
            "formats": [
                {
                    "format_id": "1080-video",
                    "url": "https://test/1080-video.mp4",
                    "height": 1080,
                    "vcodec": "avc1",
                    "acodec": "none",
                },
                {
                    "format_id": "720-muxed",
                    "url": "https://test/720-muxed.mp4",
                    "height": 720,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                },
            ]
        }
        result = _build_quality_options(info)
        assert [option.id for option in result] == ["ytdlp_1080", "ytdlp_720"]

    def test_filters_low_quality(self):
        from atv_player.yt_dlp_service import _build_quality_options
        info = {
            "formats": [
                {"format_id": "240", "url": "https://test/240.mp4", "height": 240, "vcodec": "avc1"},
                {"format_id": "720", "url": "https://test/720.mp4", "height": 720, "vcodec": "avc1"},
            ]
        }
        result = _build_quality_options(info)
        assert len(result) == 1
        assert result[0].height == 720

    def test_filters_audio_only(self):
        from atv_player.yt_dlp_service import _build_quality_options
        info = {
            "formats": [
                {"format_id": "audio", "url": "https://test/a.mp4", "height": None, "vcodec": "none"},
                {"format_id": "720", "url": "https://test/720.mp4", "height": 720, "vcodec": "avc1"},
            ]
        }
        result = _build_quality_options(info)
        assert len(result) == 1

    def test_deduplicates(self):
        from atv_player.yt_dlp_service import _build_quality_options
        info = {
            "formats": [
                {"format_id": "720", "url": "https://test/720.mp4", "height": 720, "vcodec": "avc1"},
                {"format_id": "720", "url": "https://test/720b.mp4", "height": 720, "vcodec": "avc1"},
            ]
        }
        result = _build_quality_options(info)
        assert len(result) == 1

    def test_sorted_by_height_desc(self):
        from atv_player.yt_dlp_service import _build_quality_options
        info = {
            "formats": [
                {"format_id": "480", "url": "https://test/480.mp4", "height": 480, "vcodec": "avc1"},
                {"format_id": "1080", "url": "https://test/1080.mp4", "height": 1080, "vcodec": "avc1"},
                {"format_id": "720", "url": "https://test/720.mp4", "height": 720, "vcodec": "avc1"},
            ]
        }
        result = _build_quality_options(info)
        heights = [q.height for q in result]
        assert heights == [1080, 720, 480]

    def test_no_url_still_exposes_height_for_re_resolve(self):
        from atv_player.yt_dlp_service import _build_quality_options
        info = {
            "formats": [
                {"format_id": "720", "height": 720, "vcodec": "avc1"},
            ]
        }
        result = _build_quality_options(info)
        assert [option.id for option in result] == ["ytdlp_720"]

    def test_youtube_quality_options_prefer_same_height_muxed_url_for_fast_switch(self):
        from atv_player.yt_dlp_service import _build_quality_options
        info = {
            "extractor": "youtube",
            "formats": [
                {
                    "format_id": "397",
                    "url": "https://stream.test/video-480.mp4",
                    "height": 480,
                    "tbr": 900,
                    "vcodec": "avc1.4d401f",
                    "acodec": "none",
                },
                {
                    "format_id": "94",
                    "url": "https://stream.test/480-master.m3u8",
                    "height": 480,
                    "tbr": 600,
                    "vcodec": "avc1.4d401f",
                    "acodec": "mp4a.40.2",
                },
            ],
        }

        result = _build_quality_options(info)

        assert [option.id for option in result] == ["ytdlp_480"]
        assert result[0].url == "https://stream.test/480-master.m3u8"
        assert result[0].ytdl_format == "best[height<=480]/bestvideo[height<=480]+bestaudio/bestvideo+bestaudio/best"


class TestBuildSubtitleOptions:
    def test_manual_and_auto(self):
        from atv_player.yt_dlp_service import _build_subtitle_options
        info = {
            "subtitles": {
                "en": [{"url": "https://sub/en.srt", "ext": "srt"}],
            },
            "automatic_captions": {
                "zh-Hans": [{"url": "https://sub/zh.vtt", "ext": "vtt"}],
            },
        }
        result = _build_subtitle_options(info)
        assert len(result) == 2
        en_sub = next(s for s in result if s.lang == "en")
        assert "自动生成" not in en_sub.name
        zh_sub = next(s for s in result if s.lang == "zh-Hans")
        assert "自动生成" in zh_sub.name

    def test_deduplicates_urls(self):
        from atv_player.yt_dlp_service import _build_subtitle_options
        info = {
            "subtitles": {
                "en": [{"url": "https://sub/en.srt", "ext": "srt"}],
            },
            "automatic_captions": {
                "en": [{"url": "https://sub/en.srt", "ext": "srt"}],
            },
        }
        result = _build_subtitle_options(info)
        assert len(result) == 1

    def test_empty(self):
        from atv_player.yt_dlp_service import _build_subtitle_options
        info: dict = {}
        result = _build_subtitle_options(info)
        assert len(result) == 0

    def test_prefers_configured_zh_cn_when_youtube_returns_zh_hans(self):
        from atv_player.yt_dlp_service import _build_subtitle_options
        info = {
            "subtitles": {
                "en": [{"url": "https://sub/en.srt", "ext": "srt"}],
                "zh-Hans": [{"url": "https://sub/zh.srt", "ext": "srt"}],
            },
        }

        result = _build_subtitle_options(info, preferred_lang="zh-CN")

        assert [subtitle.lang for subtitle in result] == ["zh-Hans", "en"]

    def test_prefers_supported_direct_subtitle_format_per_language(self):
        from atv_player.yt_dlp_service import _build_subtitle_options
        info = {
            "automatic_captions": {
                "en": [
                    {"url": "https://sub/en.json3", "ext": "json3"},
                    {"url": "https://sub/en.ttml", "ext": "ttml"},
                    {"url": "https://sub/en.vtt", "ext": "vtt"},
                ],
            },
        }

        result = _build_subtitle_options(info)

        assert [(subtitle.url, subtitle.format) for subtitle in result] == [
            ("https://sub/en.vtt", "vtt"),
        ]

    def test_skips_translated_youtube_caption_candidates_with_tlang(self):
        from atv_player.yt_dlp_service import _build_subtitle_options
        info = {
            "automatic_captions": {
                "zh-Hans": [
                    {"url": "https://www.youtube.com/api/timedtext?lang=en&fmt=vtt&tlang=zh-Hans", "ext": "vtt"},
                ],
                "en": [
                    {"url": "https://www.youtube.com/api/timedtext?lang=en&fmt=vtt", "ext": "vtt"},
                ],
            },
        }

        result = _build_subtitle_options(info)

        assert [(subtitle.lang, subtitle.url) for subtitle in result] == [
            ("en", "https://www.youtube.com/api/timedtext?lang=en&fmt=vtt"),
        ]
