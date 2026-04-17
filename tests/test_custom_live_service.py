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


def test_custom_live_service_merges_duplicate_group_channels_into_one_item_and_request(
    tmp_path: Path,
) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "自定义远程")
    repo.update_source(
        source.id,
        display_name="自定义远程",
        enabled=True,
        source_value="https://example.com/live.m3u",
        cache_text=(
            "#EXTM3U\n"
            "#EXTINF:-1 group-title=\"央视频道\" "
            "http-header=\"Referer=https://origin-a.example/\",CCTV1综合\n"
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


def test_custom_live_service_exposes_live_source_management_methods(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    service.add_remote_source("https://example.com/live.m3u", "远程源")
    service.add_local_source("/tmp/local.m3u", "本地源")
    manual = service.add_manual_source("手动源")

    sources = [item for item in service.list_sources() if item.display_name in {"远程源", "本地源", "手动源"}]

    assert [(item.source_type, item.source_value, item.display_name) for item in sources] == [
        ("remote", "https://example.com/live.m3u", "远程源"),
        ("local", "/tmp/local.m3u", "本地源"),
        ("manual", "", "手动源"),
    ]
    assert service.list_manual_entries(manual.id) == []


def test_custom_live_service_renames_source_without_changing_other_fields(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("remote", "https://example.com/live.m3u", "旧名称")
    repo.update_source(
        source.id,
        display_name="旧名称",
        enabled=False,
        source_value="https://example.com/live.m3u",
        cache_text="#EXTM3U",
        last_error="timeout",
        last_refreshed_at=9,
    )
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    service.rename_source(source.id, "新名称")

    saved = repo.get_source(source.id)
    assert saved.display_name == "新名称"
    assert saved.enabled is False
    assert saved.source_value == "https://example.com/live.m3u"
    assert saved.cache_text == "#EXTM3U"
    assert saved.last_error == "timeout"
    assert saved.last_refreshed_at == 9


def test_custom_live_service_deletes_source(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("manual", "", "手动源")
    service = CustomLiveService(repo, http_client=FakeHttpClient())

    service.delete_source(source.id)

    assert [item.id for item in service.list_sources()] == [1]


def test_custom_live_service_manages_manual_entries(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    service = CustomLiveService(repo, http_client=FakeHttpClient())
    source = service.add_manual_source("手动源")
    first = service.add_manual_entry(
        source.id,
        group_name="央视",
        channel_name="CCTV-1",
        stream_url="https://live.example/1.m3u8",
    )
    second = service.add_manual_entry(
        source.id,
        group_name="央视",
        channel_name="CCTV-2",
        stream_url="https://live.example/2.m3u8",
    )

    service.update_manual_entry(
        first.id,
        group_name="央视频道",
        channel_name="CCTV-1综合",
        stream_url="https://live.example/cctv1hd.m3u8",
    )
    service.move_manual_entry(second.id, -1)
    service.delete_manual_entry(first.id)

    entries = service.list_manual_entries(source.id)

    assert [(item.id, item.channel_name, item.sort_order) for item in entries] == [
        (second.id, "CCTV-2", 0)
    ]


def test_custom_live_service_propagates_manual_entry_logo_to_items_and_request(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    service = CustomLiveService(repo, http_client=FakeHttpClient())
    source = service.add_manual_source("手动源")
    entry = service.add_manual_entry(
        source.id,
        group_name="",
        channel_name="CCTV-1",
        stream_url="https://live.example/cctv1.m3u8",
        logo_url="https://img.example/cctv1.png",
    )

    items, total = service.load_items(f"custom:{source.id}", 1)
    request = service.build_request(f"custom-channel:{source.id}:manual-{entry.id}")

    assert total == 1
    assert items[0].vod_pic == "https://img.example/cctv1.png"
    assert request.vod.vod_pic == "https://img.example/cctv1.png"
