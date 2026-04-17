from pathlib import Path

from atv_player.live_source_repository import LiveSourceRepository


def test_live_source_repository_inserts_default_example_source(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")

    sources = repo.list_sources()

    assert len(sources) == 1
    assert sources[0].display_name == "示例直播源"
    assert sources[0].source_type == "remote"
    assert sources[0].source_value == "https://raw.githubusercontent.com/Rivens7/Livelist/refs/heads/main/IPTV.m3u"
    assert sources[0].enabled is True
    assert sources[0].is_default is True


def test_live_source_repository_round_trips_source_updates_and_manual_entries(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("manual", "", "自建直播")
    repo.update_source(
        source.id,
        display_name="自建频道",
        enabled=False,
        source_value="",
        cache_text="#EXTM3U",
        last_error="解析失败",
        last_refreshed_at=123,
    )
    repo.add_manual_entry(
        source.id,
        group_name="央视",
        channel_name="CCTV-1",
        stream_url="https://live.example/cctv1.m3u8",
    )

    saved = [item for item in repo.list_sources() if item.id == source.id][0]
    entries = repo.list_manual_entries(source.id)

    assert saved.display_name == "自建频道"
    assert saved.enabled is False
    assert saved.cache_text == "#EXTM3U"
    assert saved.last_error == "解析失败"
    assert saved.last_refreshed_at == 123
    assert [(item.group_name, item.channel_name, item.stream_url) for item in entries] == [
        ("央视", "CCTV-1", "https://live.example/cctv1.m3u8")
    ]


def test_live_source_repository_moves_sources_and_entries_in_sort_order(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    first = repo.add_source("remote", "https://example.com/a.m3u", "A")
    second = repo.add_source("remote", "https://example.com/b.m3u", "B")
    repo.move_source(second.id, -1)
    manual = repo.add_source("manual", "", "手动")
    repo.add_manual_entry(
        manual.id,
        group_name="",
        channel_name="一台",
        stream_url="https://live.example/1.m3u8",
    )
    second_entry = repo.add_manual_entry(
        manual.id,
        group_name="",
        channel_name="二台",
        stream_url="https://live.example/2.m3u8",
    )
    repo.move_manual_entry(second_entry.id, -1)

    sources = [item.display_name for item in repo.list_sources() if item.display_name in {"A", "B"}]
    entries = [item.channel_name for item in repo.list_manual_entries(manual.id)]

    assert sources == ["B", "A"]
    assert entries == ["二台", "一台"]
