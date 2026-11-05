import os
import time
from pathlib import Path


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_build_cache_summary_counts_categories_and_total(tmp_path: Path) -> None:
    from atv_player.cache_management import build_cache_summary

    _write_file(tmp_path / "plugins" / "plugin_1.py", b"1234")
    _write_file(tmp_path / "posters" / "poster.img", b"123")
    _write_file(tmp_path / "metadata" / "search" / "douban" / "hit.json", b"12")
    _write_file(tmp_path / "danmaku" / "episode.ass", b"12345")
    _write_file(tmp_path / "playlists" / "clean.m3u8", b"123456")
    _write_file(tmp_path / "subtitles" / "inline.srt", b"1234567")
    _write_file(tmp_path / "media" / "abc" / "b0", b"1234567890")
    _write_file(tmp_path / "scratch" / "unknown.bin", b"12345678")
    _write_file(tmp_path / "loose.tmp", b"1")

    summary = build_cache_summary(tmp_path)
    by_id = {category.id: category for category in summary.categories}

    assert by_id["plugins"].size_bytes == 4
    assert by_id["plugins"].file_count == 1
    assert by_id["posters"].size_bytes == 3
    assert by_id["metadata"].size_bytes == 2
    assert by_id["danmaku"].size_bytes == 5
    assert by_id["playback"].size_bytes == 13
    assert by_id["playback"].file_count == 2
    assert by_id["media"].size_bytes == 10
    assert by_id["media"].file_count == 1
    assert by_id["other"].size_bytes == 9
    assert by_id["other"].file_count == 2
    assert summary.total_size_bytes == 46
    assert summary.total_file_count == 9


def test_clear_cache_category_removes_only_that_category(tmp_path: Path) -> None:
    from atv_player.cache_management import clear_cache_category

    _write_file(tmp_path / "posters" / "poster.img", b"poster")
    _write_file(tmp_path / "plugins" / "plugin_1.py", b"plugin")

    clear_cache_category("posters", tmp_path)

    assert (tmp_path / "posters").is_dir()
    assert list((tmp_path / "posters").iterdir()) == []
    assert (tmp_path / "plugins" / "plugin_1.py").read_bytes() == b"plugin"


def test_clear_playback_category_clears_playlists_and_subtitles(tmp_path: Path) -> None:
    from atv_player.cache_management import clear_cache_category

    _write_file(tmp_path / "playlists" / "clean.m3u8", b"playlist")
    _write_file(tmp_path / "subtitles" / "inline.srt", b"subtitle")

    clear_cache_category("playback", tmp_path)

    assert list((tmp_path / "playlists").iterdir()) == []
    assert list((tmp_path / "subtitles").iterdir()) == []


def test_clear_all_cache_removes_cache_root_contents(tmp_path: Path) -> None:
    from atv_player.cache_management import clear_all_cache

    _write_file(tmp_path / "posters" / "poster.img", b"poster")
    _write_file(tmp_path / "scratch" / "unknown.bin", b"unknown")
    _write_file(tmp_path / "loose.tmp", b"loose")

    clear_all_cache(tmp_path)

    assert tmp_path.is_dir()
    assert list(tmp_path.iterdir()) == []


def test_clear_cache_older_than_removes_only_old_files_and_empty_dirs(tmp_path: Path) -> None:
    from atv_player.cache_management import build_cache_summary, clear_cache_older_than

    old_file = tmp_path / "posters" / "old.img"
    new_file = tmp_path / "posters" / "new.img"
    old_nested_file = tmp_path / "metadata" / "detail" / "douban" / "old.json"
    old_other_file = tmp_path / "scratch" / "old.bin"
    _write_file(old_file, b"old")
    _write_file(new_file, b"newer")
    _write_file(old_nested_file, b"metadata")
    _write_file(old_other_file, b"other")

    now = time.time()
    old_mtime = now - (8 * 24 * 60 * 60)
    new_mtime = now - (2 * 24 * 60 * 60)
    for path in (old_file, old_nested_file, old_other_file):
        os.utime(path, (old_mtime, old_mtime))

    os.utime(new_file, (new_mtime, new_mtime))

    result = clear_cache_older_than(7, tmp_path, now=now)

    assert result.removed_file_count == 3
    assert result.removed_size_bytes == 16
    assert old_file.exists() is False
    assert old_nested_file.exists() is False
    assert old_other_file.exists() is False
    assert new_file.read_bytes() == b"newer"
    assert (tmp_path / "metadata").is_dir()
    assert (tmp_path / "metadata" / "detail").exists() is False
    assert build_cache_summary(tmp_path).total_file_count == 1


def test_clear_cache_older_than_rejects_non_positive_days(tmp_path: Path) -> None:
    from atv_player.cache_management import clear_cache_older_than

    try:
        clear_cache_older_than(0, tmp_path)
    except ValueError as exc:
        assert str(exc) == "清理天数必须大于 0"
    else:
        raise AssertionError("expected ValueError")
