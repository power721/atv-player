"""MediaBlockCache 单元测试:块补全/切片/meta 重建/淘汰/持久化。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from atv_player.proxy.block_cache import BLOCK_SIZE, MediaBlockCache

_K = "drive-1|/剧/第01集.mkv|quark|"


def _make_cache(tmp_path: Path, **kwargs) -> MediaBlockCache:
    return MediaBlockCache(tmp_path / "media", **kwargs)


def _deterministic_body(total: int) -> bytes:
    return bytes((i * 31 + 7) & 0xFF for i in range(total))


def test_meta_roundtrip_and_identity_change(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    cache.write_meta(_K, 1000, "video/mp4", 0)

    meta = cache.lookup(_K)
    assert meta is not None
    assert meta.total_length == 1000
    assert meta.content_type == "video/mp4"
    assert meta.stream_offset == 0
    assert meta.block_size == BLOCK_SIZE

    # 流偏移(obfuscated mkv)一并入 meta
    cache.write_meta(_K, 1000, "video/mp4", 8)
    assert cache.lookup(_K).stream_offset == 8

    # 同身份但总长变化 → 条目重建,旧块作废
    cache.store_segment(_K, 0, b"x" * 1000, 1000)
    assert cache.blocks_complete(_K, 0, 999)
    cache.write_meta(_K, 4096, "video/mp4", 8)
    assert cache.lookup(_K).total_length == 4096
    assert not cache.blocks_complete(_K, 0, 999)


def test_segments_fill_blocks_and_serve_range_slices(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    total = 700 * 1024
    body = _deterministic_body(total)
    cache.write_meta(_K, total, "video/mp4", 0)

    # 一段不齐块边界的分片:内部整块(块1)自动补全,两端半块(块0/块2)不算完整
    cache.store_segment(_K, 100 * 1024, body[100 * 1024 : 600 * 1024], total)
    assert cache.blocks_complete(_K, BLOCK_SIZE, 2 * BLOCK_SIZE - 1)
    assert not cache.blocks_complete(_K, 0, 2 * BLOCK_SIZE - 1)
    assert not cache.blocks_complete(_K, 2 * BLOCK_SIZE, 3 * BLOCK_SIZE - 1)

    # 补齐两端
    cache.store_segment(_K, 0, body[: 100 * 1024], total)
    cache.store_segment(_K, 600 * 1024, body[600 * 1024 :], total)
    assert cache.blocks_complete(_K, 0, total - 1)
    # end 超出总长按 total-1 钳制
    assert cache.blocks_complete(_K, 0, total + 10_000)

    served = bytearray()
    count = cache.serve_range(_K, 123, 560 * 1024, served.extend)
    assert count == 560 * 1024 - 123 + 1
    assert bytes(served) == body[123 : 560 * 1024 + 1]

    # 空区间
    assert cache.serve_range(_K, 10, 9, lambda _chunk: None) == 0


def test_seek_gap_does_not_falsely_complete(tmp_path: Path) -> None:
    """seek 产生的不连续写入:块内留洞时,稀疏文件大小会到块尾,必须判不完整。"""
    cache = _make_cache(tmp_path)
    total = 700 * 1024
    body = _deterministic_body(total)
    cache.write_meta(_K, total, "video/mp4", 0)

    cache.store_segment(_K, 0, body[: 100 * 1024], total)
    cache.store_segment(_K, 200 * 1024, body[200 * 1024 : 300 * 1024], total)
    assert not cache.blocks_complete(_K, 0, BLOCK_SIZE - 1)

    # 补上中段空洞后应完整
    cache.store_segment(_K, 100 * 1024, body[100 * 1024 : 200 * 1024], total)
    assert cache.blocks_complete(_K, 0, BLOCK_SIZE - 1)
    served = bytearray()
    cache.serve_range(_K, 0, BLOCK_SIZE - 1, served.extend)
    assert bytes(served) == body[:BLOCK_SIZE]


def test_disabled_cache_is_noop(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    cache.configure(False, 1024 * 1024)
    cache.write_meta(_K, 4096, "video/mp4", 0)
    assert cache.lookup(_K) is None
    cache.store_segment(_K, 0, b"abcd" * 1024, 4096)
    assert not cache.blocks_complete(_K, 0, 4095)


def test_blocks_survive_restart(tmp_path: Path) -> None:
    total = 300 * 1024
    body = _deterministic_body(total)
    cache = _make_cache(tmp_path)
    cache.write_meta(_K, total, "video/mp4", 0)
    cache.store_segment(_K, 0, body, total)
    assert cache.blocks_complete(_K, 0, total - 1)

    revived = _make_cache(tmp_path)
    assert revived.lookup(_K) is not None
    assert revived.blocks_complete(_K, 0, total - 1)
    served = bytearray()
    assert revived.serve_range(_K, 0, total - 1, served.extend) == total
    assert bytes(served) == body


def test_trim_evicts_oldest_entry_first(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    cache.configure(True, 600 * 1024)
    now = time.time()
    for key, age_seconds in (("old", 3600), ("mid", 1800), ("new", 60)):
        cache.write_meta(key, BLOCK_SIZE, "video/mp4", 0)
        cache.store_segment(key, 0, _deterministic_body(BLOCK_SIZE), BLOCK_SIZE)
        entry = cache._entry_dir(key)
        os.utime(entry, (now - age_seconds, now - age_seconds))

    cache._trim()

    assert not cache._entry_dir("old").exists()
    assert cache._entry_dir("mid").exists()
    assert cache._entry_dir("new").exists()


def test_trim_cleans_stale_tmp_files(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    cache.write_meta(_K, BLOCK_SIZE, "video/mp4", 0)
    entry = cache._entry_dir(_K)
    stale = entry / "b0.tmp"
    fresh = entry / "b1.tmp"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"y")
    now = time.time()
    os.utime(stale, (now - 120, now - 120))
    os.utime(fresh, (now - 5, now - 5))

    cache._trim()

    assert not stale.exists()
    assert fresh.exists()


def test_write_trigger_schedules_background_trim(tmp_path: Path, monkeypatch) -> None:
    import atv_player.proxy.block_cache as block_cache_module

    monkeypatch.setattr(block_cache_module, "TRIM_TRIGGER_BYTES", BLOCK_SIZE)
    cache = _make_cache(tmp_path)
    cache.write_meta(_K, BLOCK_SIZE, "video/mp4", 0)
    entry_marker = cache._entry_dir(_K) / "meta.tmp"
    entry_marker.write_bytes(b"stale")
    os.utime(entry_marker, (time.time() - 120, time.time() - 120))

    cache.store_segment(_K, 0, _deterministic_body(BLOCK_SIZE), BLOCK_SIZE)

    deadline = time.time() + 5
    while time.time() < deadline and entry_marker.exists():
        time.sleep(0.05)
    assert not entry_marker.exists()
