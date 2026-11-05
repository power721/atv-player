"""网盘媒体磁盘块缓存:256KiB 块对齐落盘,按文件身份(而非直链 URL)做键。

直链每次解析都会变(带签名),所以缓存键由调用方用稳定身份(网盘资源 ID+路径等)
构造;条目布局 ``<root>/<sha1(key)>/{meta, ext, b0, b1, ...}``:

- ``meta`` 五行:版本 / 总长 / Content-Type / 块大小 / 流偏移(obfuscated mkv)
- ``ext`` 覆盖区间日志,每行 ``<start> <end>``(流坐标,含首不含尾)
- ``b<i>`` 块文件,流坐标第 i 块

完整性以 ``ext`` 区间并集为准(先写块数据、后追加日志;崩溃夹在中间只是保守判
不完整)。按大小判不可靠:os.pwrite 部分写入会产生稀疏空洞,文件大小恰好等于
写入末端,seek 产生的中段洞会被误判完整。

淘汰按条目(整个视频)最新 mtime 做 LRU,累计写入 16MiB 触发后台 trim 至配额
的 90%;trim 顺带清理超过宽限期的 ``.tmp`` 残留。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

BLOCK_SIZE = 256 * 1024
TRIM_TRIGGER_BYTES = 16 * 1024 * 1024
TRIM_TARGET_RATIO = 0.9
DEFAULT_QUOTA_BYTES = 2048 * 1024 * 1024
TMP_GRACE_SECONDS = 60.0

_META_VERSION = 1


@dataclass(frozen=True)
class MediaCacheMeta:
    total_length: int
    content_type: str
    stream_offset: int
    block_size: int


class MediaBlockCache:
    """线程安全;写入来自代理 worker 线程,读取来自请求处理线程。"""

    def __init__(self, root: Path, *, block_size: int = BLOCK_SIZE) -> None:
        self._root = Path(root)
        self._block_size = max(4096, int(block_size))
        self._enabled = True
        self._quota_bytes = DEFAULT_QUOTA_BYTES
        self._lock = threading.Lock()
        self._written_since_trim = 0
        self._trim_running = False
        # key -> 已合并排序的覆盖区间 [(start, end), ...];None 表示尚未从 ext 加载
        self._extents: dict[str, list[tuple[int, int]] | None] = {}

    @property
    def root(self) -> Path:
        return self._root

    @property
    def block_size(self) -> int:
        return self._block_size

    def configure(self, enabled: bool, quota_bytes: int) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._quota_bytes = max(self._block_size, int(quota_bytes))

    def _entry_dir(self, key: str) -> Path:
        return self._root / hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _meta_path(self, entry: Path) -> Path:
        return entry / "meta"

    def _ext_path(self, entry: Path) -> Path:
        return entry / "ext"

    # ── meta ──────────────────────────────────────────────────────────────

    def lookup(self, key: str) -> MediaCacheMeta | None:
        if not key:
            return None
        try:
            raw = self._meta_path(self._entry_dir(key)).read_text("utf-8")
        except OSError:
            return None
        lines = raw.splitlines()
        if len(lines) < 4:
            return None
        try:
            version = int(lines[0])
            total = int(lines[1])
            block = int(lines[3])
            offset = int(lines[4]) if len(lines) > 4 else 0
        except ValueError:
            return None
        if version != _META_VERSION or total <= 0 or block <= 0:
            return None
        return MediaCacheMeta(
            total_length=total,
            content_type=lines[2],
            stream_offset=max(0, offset),
            block_size=block,
        )

    def write_meta(
        self,
        key: str,
        total_length: int,
        content_type: str,
        stream_offset: int = 0,
    ) -> None:
        if not key or total_length <= 0:
            return
        with self._lock:
            enabled = self._enabled
        if not enabled:
            return
        entry = self._entry_dir(key)
        existing = self.lookup(key)
        if existing is not None and (
            existing.total_length != total_length
            or existing.stream_offset != max(0, stream_offset)
            or existing.block_size != self._block_size
        ):
            # 同一身份但内容变了(文件被替换/覆盖):整条目作废重来。
            with self._lock:
                self._extents.pop(key, None)
            self._remove_entry(entry)
            logger.info(
                "媒体缓存条目身份变更,重建 key=%s total=%s->%s",
                key[:12],
                existing.total_length,
                total_length,
                extra={"log_category": "network", "log_source": "app"},
            )
        try:
            entry.mkdir(parents=True, exist_ok=True)
            text = (
                f"{_META_VERSION}\n{total_length}\n{content_type}\n"
                f"{self._block_size}\n{max(0, stream_offset)}\n"
            )
            tmp = self._meta_path(entry).with_suffix(".meta.tmp")
            tmp.write_text(text, "utf-8")
            os.replace(tmp, self._meta_path(entry))
        except OSError as exc:
            logger.warning(
                "媒体缓存 meta 写入失败 key=%s error=%s",
                key[:12],
                exc,
                extra={"log_category": "network", "log_source": "app"},
            )

    # ── 覆盖区间 ──────────────────────────────────────────────────────────

    def _load_extents(self, key: str) -> list[tuple[int, int]]:
        with self._lock:
            cached = self._extents.get(key)
            if cached is not None:
                return cached
            # 锁内做读盘+合并:与 _record_extent 的读-合-写互斥,防止并发丢区间。
            extents = self._read_extents_file(key)
            merged = _merge_extents(extents)
            self._extents[key] = merged
            return merged

    def _read_extents_file(self, key: str) -> list[tuple[int, int]]:
        """读 ext 日志;调用方须持有 self._lock。"""
        extents: list[tuple[int, int]] = []
        try:
            raw = self._ext_path(self._entry_dir(key)).read_text("utf-8")
        except OSError:
            return extents
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                extents.append((int(parts[0]), int(parts[1])))
            except ValueError:
                continue
        return extents

    def _record_extent(self, key: str, start: int, end: int) -> None:
        """合并进内存区间表并追加到 ext 日志;调用方保证 entry 目录存在。"""
        with self._lock:
            cached = self._extents.get(key)
            if cached is not None:
                base = cached
            else:
                base = _merge_extents(self._read_extents_file(key))
            merged = _merge_extents([*base, (start, end)])
            ext_path = self._ext_path(self._entry_dir(key))
            try:
                with open(ext_path, "a", encoding="utf-8") as fh:
                    fh.write(f"{start} {end}\n")
            except OSError:
                return  # 日志失败:该区间不计数,块数据仍在,保守判不完整
            self._extents[key] = merged

    # ── 块读写 ────────────────────────────────────────────────────────────

    def blocks_complete(self, key: str, start: int, end: int) -> bool:
        """[start, end](含端点,流坐标)是否被完整覆盖;要求 meta 先命中。"""
        meta = self.lookup(key)
        if meta is None or meta.block_size != self._block_size:
            return False
        last = min(end, meta.total_length - 1)
        if last < start:
            return True  # 空区间视为完整
        extents = self._load_extents(key)
        if not _extents_cover(extents, start, last):
            return False
        entry = self._entry_dir(key)
        for index in range(start // self._block_size, last // self._block_size + 1):
            block_start = index * self._block_size
            block_end = min(block_start + self._block_size, meta.total_length)
            needed = _max_covered_in_block(extents, block_start, block_end)
            if needed <= 0:
                continue
            try:
                size = (entry / f"b{index}").stat().st_size
            except OSError:
                return False
            # size 校验只防"日志声称覆盖但块文件被截断"(崩溃残留),不要求整块填满
            if size < needed:
                return False
        return True

    def store_segment(
        self, key: str, start: int, data: bytes, total_length: int
    ) -> None:
        """把一段连续流坐标分片写进覆盖的块文件(pwrite 到块内偏移,不 fsync)。"""
        if not key or not data or total_length <= 0:
            return
        meta = self.lookup(key)
        if (
            meta is None
            or meta.block_size != self._block_size
            or meta.total_length != total_length
        ):
            return
        entry = self._entry_dir(key)
        try:
            entry.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        written = 0
        first = start // self._block_size
        last = (start + len(data) - 1) // self._block_size
        for index in range(first, last + 1):
            block_start = index * self._block_size
            overlap_start = max(start, block_start)
            overlap_end = min(start + len(data), block_start + self._block_size)
            if overlap_end <= overlap_start:
                continue
            required = _block_required_size(index, meta.total_length, self._block_size)
            if required is None:
                continue
            chunk = data[overlap_start - start : overlap_end - start]
            if overlap_start - block_start + len(chunk) > required:
                chunk = chunk[: max(0, required - (overlap_start - block_start))]
                if not chunk:
                    continue
            try:
                fd = os.open(entry / f"b{index}", os.O_RDWR | os.O_CREAT, 0o644)
            except OSError:
                continue
            try:
                os.pwrite(fd, chunk, overlap_start - block_start)
                written += len(chunk)
            except OSError:
                pass
            finally:
                os.close(fd)
        if written:
            # 先写数据后记日志:崩溃夹在中间只会保守判不完整,不会误判完整。
            self._record_extent(key, start, start + len(data))
            self._touch(entry)
            self._account_write(written)

    def serve_range(self, key: str, start: int, end: int, write) -> int:
        """按块读出 [start, end] 切片并回调 write(chunk);返回写出的字节数。"""
        meta = self.lookup(key)
        if meta is None:
            return 0
        last = min(end, meta.total_length - 1)
        if last < start:
            return 0
        entry = self._entry_dir(key)
        served = 0
        index = start // self._block_size
        while index * self._block_size <= last:
            block_start = index * self._block_size
            slice_start = max(start, block_start) - block_start
            slice_end = min(last + 1, block_start + self._block_size) - block_start
            try:
                with open(entry / f"b{index}", "rb") as fh:
                    fh.seek(slice_start)
                    chunk = fh.read(slice_end - slice_start)
            except OSError:
                if served:
                    raise
                return 0
            if len(chunk) != slice_end - slice_start:
                if served:
                    raise OSError(f"short block read: b{index}")
                return 0
            write(chunk)
            served += len(chunk)
            index += 1
        self._touch(entry)
        return served

    # ── 淘汰 ──────────────────────────────────────────────────────────────

    def clear(self) -> None:
        with self._lock:
            self._written_since_trim = 0
            self._extents.clear()
        shutil.rmtree(self._root, ignore_errors=True)

    def _touch(self, entry: Path) -> None:
        try:
            os.utime(entry, None)
        except OSError:
            pass

    def _account_write(self, count: int) -> None:
        schedule = False
        with self._lock:
            self._written_since_trim += count
            if (
                self._written_since_trim >= TRIM_TRIGGER_BYTES
                and not self._trim_running
            ):
                schedule = True
                self._trim_running = True
                self._written_since_trim = 0
        if schedule:
            threading.Thread(
                target=self._trim,
                daemon=True,
                name="media-block-cache-trim",
            ).start()

    def _trim(self) -> None:
        try:
            quota = self._quota_bytes
            entries = self._collect_entries()
            self._cleanup_tmp(entries)
            total = sum(size for _mtime, size in entries.values())
            if total <= quota:
                return
            target = int(quota * TRIM_TARGET_RATIO)
            removed_size = 0
            ordered = sorted(entries.items(), key=lambda item: item[1][0])
            for path, (mtime, size) in ordered:
                if total - removed_size <= target:
                    break
                self._remove_entry(path)
                removed_size += size
                self._forget_entry_dir(path)
                logger.info(
                    "媒体缓存淘汰条目 dir=%s size=%.1fMB last_use=%s",
                    path.name,
                    size / 1024 / 1024,
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                    extra={"log_category": "network", "log_source": "app"},
                )
            if removed_size:
                logger.info(
                    "媒体缓存 trim 完成 total=%.1fMB quota=%.1fMB removed=%.1fMB",
                    total / 1024 / 1024,
                    quota / 1024 / 1024,
                    removed_size / 1024 / 1024,
                    extra={"log_category": "network", "log_source": "app"},
                )
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底
            logger.warning(
                "媒体缓存 trim 失败 error=%s",
                exc,
                extra={"log_category": "network", "log_source": "app"},
            )
        finally:
            with self._lock:
                self._trim_running = False

    def _forget_entry_dir(self, entry: Path) -> None:
        name = entry.name
        with self._lock:
            for key, cached in list(self._extents.items()):
                if cached is None:
                    continue
                if self._entry_dir(key).name == name:
                    self._extents.pop(key, None)

    def _collect_entries(self) -> dict[Path, tuple[float, int]]:
        entries: dict[Path, tuple[float, int]] = {}
        try:
            children = list(self._root.iterdir())
        except OSError:
            return entries
        now = time.time()
        for entry in children:
            if not entry.is_dir():
                continue
            size = 0
            latest = 0.0
            try:
                for item in entry.iterdir():
                    try:
                        stat = item.stat()
                    except OSError:
                        continue
                    size += stat.st_size
                    latest = max(latest, stat.st_mtime)
            except OSError:
                continue
            try:
                latest = max(latest, entry.stat().st_mtime)
            except OSError:
                pass
            if latest <= 0:
                latest = now
            entries[entry] = (latest, size)
        return entries

    def _cleanup_tmp(self, entries: dict[Path, tuple[float, int]]) -> None:
        now = time.time()
        for entry in entries:
            try:
                items = list(entry.iterdir())
            except OSError:
                continue
            for item in items:
                if not item.name.endswith(".tmp"):
                    continue
                try:
                    if now - item.stat().st_mtime > TMP_GRACE_SECONDS:
                        item.unlink(missing_ok=True)
                except OSError:
                    continue

    @staticmethod
    def _remove_entry(entry: Path) -> None:
        shutil.rmtree(entry, ignore_errors=True)


def _block_required_size(index: int, total_length: int, block_size: int) -> int | None:
    """该块应有的字节数;块起点超出文件长度返回 None。"""
    block_start = index * block_size
    if block_start >= total_length:
        return None
    return min(block_size, total_length - block_start)


def _merge_extents(extents: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(extents):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _extents_cover(extents: list[tuple[int, int]], start: int, end: int) -> bool:
    """[start, end](含端点)是否被区间并集完全覆盖。"""
    cursor = start
    for extent_start, extent_end in extents:
        if extent_start > cursor:
            return False
        if extent_end > cursor:
            cursor = extent_end - 1  # 含端点换算成含首不含尾
        if cursor >= end:
            return True
    return cursor >= end


def _max_covered_in_block(
    extents: list[tuple[int, int]], block_start: int, block_end: int
) -> int:
    """块内被覆盖到的最大偏移(相对块起点);0 表示该块无覆盖。"""
    covered_to = block_start
    for extent_start, extent_end in extents:
        if extent_end <= block_start:
            continue
        if extent_start >= block_end:
            break
        covered_to = max(covered_to, min(extent_end, block_end))
    return covered_to - block_start
