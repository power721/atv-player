from pathlib import Path

from atv_player.custom_live_service import CustomLiveService
from atv_player.live_source_repository import LiveSourceRepository


class FakeHttpClient:
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[str] = []

    def get_text(self, url: str) -> str:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.text


def test_custom_live_service_lists_enabled_sources_as_categories(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    remote = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        remote.id,
        display_name="自定义远程",
        enabled=True,
        source_value=remote.source_value,
        cache_text="#EXTM3U",
        last_error="",
        last_refreshed_at=1,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    categories = service.load_categories()

    assert [(item.type_id, item.type_name) for item in categories if item.type_name == "自定义远程"] == [
        (f"custom:{remote.id}", "自定义远程")
    ]


def test_custom_live_service_prefers_cache_and_maps_groups_and_channels(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        source.id,
        display_name="自定义远程",
        enabled=True,
        source_value="https://example.com/live.m3u",
        cache_text="#EXTM3U\n#EXTINF:-1 group-title=\"央视频道\",CCTV-1\nhttps://live.example/cctv1.m3u8\n",
        last_error="",
        last_refreshed_at=1,
    )
    http = FakeHttpClient()
    service = CustomLiveService(repo, http_client=http)

    items, total = service.load_items(f"custom:{source.id}", 1)

    assert total == 1
    assert http.calls == []
    assert [(item.vod_id, item.vod_name, item.vod_tag) for item in items] == [
        (f"custom-folder:{source.id}:group-0", "央视频道", "folder")
    ]


def test_custom_live_service_loads_group_channels_and_builds_request(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        source.id,
        display_name="自定义远程",
        enabled=True,
        source_value="https://example.com/live.m3u",
        cache_text="#EXTM3U\n#EXTINF:-1 group-title=\"央视频道\",CCTV-1\nhttps://live.example/cctv1.m3u8\n",
        last_error="",
        last_refreshed_at=1,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    items, total = service.load_folder_items(f"custom-folder:{source.id}:group-0")
    request = service.build_request(f"custom-channel:{source.id}:channel-0")

    assert total == 1
    assert [(item.vod_id, item.vod_name, item.vod_tag) for item in items] == [
        (f"custom-channel:{source.id}:channel-0", "CCTV-1", "file")
    ]
    assert request.vod.vod_name == "CCTV-1"
    assert request.playlist[0].url == "https://live.example/cctv1.m3u8"
    assert request.source_mode == "custom"
    assert request.use_local_history is False


def test_custom_live_service_refresh_failure_preserves_old_cache(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        source.id,
        display_name="自定义远程",
        enabled=True,
        source_value="https://example.com/live.m3u",
        cache_text="#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://live.example/cctv1.m3u8\n",
        last_error="",
        last_refreshed_at=1,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient(error=RuntimeError("timeout")))

    items, total = service.load_items(f"custom:{source.id}", 1)

    assert total == 1
    saved = repo.get_source(source.id)
    assert items[0].vod_name == "CCTV-1"
    assert saved.cache_text.startswith("#EXTM3U")
    assert saved.last_error == ""


def test_custom_live_service_refresh_source_stores_last_error_and_keeps_cache(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        source.id,
        display_name="自定义远程",
        enabled=True,
        source_value="https://example.com/live.m3u",
        cache_text="#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://live.example/cctv1.m3u8\n",
        last_error="",
        last_refreshed_at=1,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient(error=RuntimeError("timeout")))

    try:
        service.refresh_source(source.id)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected refresh_source to raise")

    saved = repo.get_source(source.id)
    assert saved.cache_text.startswith("#EXTM3U")
    assert saved.last_error == "timeout"


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
