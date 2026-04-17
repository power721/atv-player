import sqlite3
from pathlib import Path

from atv_player.live_source_repository import LiveSourceRepository


def test_live_source_repository_inserts_default_example_source(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")

    sources = repo.list_sources()

    assert len(sources) == 1
    assert sources[0].display_name == "咪咕"
    assert sources[0].source_type == "remote"
    assert sources[0].source_value == "https://develop202.github.io/migu_video/interface.txt"
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
    repo.add_source("remote", "https://example.com/a.m3u", "A")
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


def test_live_source_repository_updates_and_deletes_manual_entries(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    manual = repo.add_source("manual", "", "手动")
    first = repo.add_manual_entry(
        manual.id,
        group_name="央视",
        channel_name="CCTV-1",
        stream_url="https://live.example/1.m3u8",
    )
    second = repo.add_manual_entry(
        manual.id,
        group_name="卫视",
        channel_name="湖南卫视",
        stream_url="https://live.example/2.m3u8",
    )

    repo.update_manual_entry(
        first.id,
        group_name="央视频道",
        channel_name="CCTV-1综合",
        stream_url="https://live.example/cctv1hd.m3u8",
    )
    repo.delete_manual_entry(first.id)

    entries = repo.list_manual_entries(manual.id)

    assert [(item.id, item.group_name, item.channel_name, item.stream_url, item.sort_order) for item in entries] == [
        (second.id, "卫视", "湖南卫视", "https://live.example/2.m3u8", 0)
    ]


def test_live_source_repository_migrates_existing_manual_entry_table_with_logo_column(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE live_source (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_value TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                last_refreshed_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                cache_text TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE live_source_entry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                group_name TEXT NOT NULL DEFAULT '',
                channel_name TEXT NOT NULL,
                stream_url TEXT NOT NULL,
                sort_order INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO live_source (source_type, source_value, display_name, enabled, sort_order, is_default)
            VALUES ('manual', '', '手动源', 1, 0, 0)
            """
        )
        conn.execute(
            """
            INSERT INTO live_source_entry (source_id, group_name, channel_name, stream_url, sort_order)
            VALUES (1, '央视', 'CCTV-1', 'https://live.example/cctv1.m3u8', 0)
            """
        )

    repo = LiveSourceRepository(db_path)

    entry = repo.list_manual_entries(1)[0]

    assert entry.logo_url == ""


def test_live_source_repository_round_trips_manual_entry_logo_url(tmp_path: Path) -> None:
    repo = LiveSourceRepository(tmp_path / "app.db")
    source = repo.add_source("manual", "", "手动源")
    entry = repo.add_manual_entry(
        source.id,
        group_name="央视",
        channel_name="CCTV-1",
        stream_url="https://live.example/cctv1.m3u8",
        logo_url="https://img.example/cctv1.png",
    )

    repo.update_manual_entry(
        entry.id,
        group_name="央视频道",
        channel_name="CCTV-1综合",
        stream_url="https://live.example/cctv1hd.m3u8",
        logo_url="https://img.example/cctv1hd.png",
    )

    saved = repo.get_manual_entry(entry.id)

    assert saved.logo_url == "https://img.example/cctv1hd.png"
