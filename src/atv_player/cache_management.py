from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from atv_player.paths import app_cache_dir


@dataclass(frozen=True)
class CacheCategory:
    id: str
    label: str
    relative_paths: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class CacheCategoryStats:
    id: str
    label: str
    paths: tuple[Path, ...]
    path_summary: str
    description: str
    size_bytes: int
    file_count: int


@dataclass(frozen=True)
class CacheSummary:
    root: Path
    categories: tuple[CacheCategoryStats, ...]
    total_size_bytes: int
    total_file_count: int


@dataclass(frozen=True)
class CacheCleanupResult:
    removed_file_count: int
    removed_size_bytes: int


CACHE_CATEGORIES: tuple[CacheCategory, ...] = (
    CacheCategory("plugins", "插件缓存", ("plugins",), "插件源码和插件运行缓存"),
    CacheCategory("posters", "海报缓存", ("posters",), "远程海报图片缓存"),
    CacheCategory("metadata", "元数据缓存", ("metadata",), "影视元数据搜索和详情缓存"),
    CacheCategory(
        "danmaku",
        "弹幕缓存",
        ("danmaku",),
        "弹幕 XML、搜索结果和 ASS 渲染缓存",
    ),
    CacheCategory(
        "playback",
        "播放缓存",
        ("playlists", "subtitles"),
        "M3U8 重写列表和插件字幕缓存",
    ),
    CacheCategory(
        "media",
        "媒体缓存",
        ("media",),
        "网盘媒体磁盘块缓存（按文件身份复用,回拖/二刷不再耗流量）",
    ),
    CacheCategory("other", "其他缓存", (), "未归入以上分类的缓存文件"),
)


def format_cache_size(size_bytes: int) -> str:
    value = float(max(0, int(size_bytes)))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def build_cache_summary(cache_root: Path | None = None) -> CacheSummary:
    root = _cache_root(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    covered_names = {
        relative_path
        for category in CACHE_CATEGORIES
        for relative_path in category.relative_paths
    }
    categories = tuple(
        _build_category_stats(root, category, covered_names)
        for category in CACHE_CATEGORIES
    )
    return CacheSummary(
        root=root,
        categories=categories,
        total_size_bytes=sum(category.size_bytes for category in categories),
        total_file_count=sum(category.file_count for category in categories),
    )


def clear_cache_category(category_id: str, cache_root: Path | None = None) -> None:
    root = _cache_root(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    category = _category_by_id(category_id)
    if category.id == "other":
        covered_names = {
            relative_path
            for item in CACHE_CATEGORIES
            for relative_path in item.relative_paths
        }
        for child in _safe_iterdir(root):
            if child.name not in covered_names:
                _remove_path(child)
        return

    for relative_path in category.relative_paths:
        path = root / relative_path
        _clear_directory_contents(path)


def clear_all_cache(cache_root: Path | None = None) -> None:
    root = _cache_root(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    for child in _safe_iterdir(root):
        _remove_path(child)


def clear_cache_older_than(
    days: int,
    cache_root: Path | None = None,
    *,
    now: float | None = None,
) -> CacheCleanupResult:
    if days <= 0:
        raise ValueError("清理天数必须大于 0")
    root = _cache_root(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    cutoff = (time.time() if now is None else now) - (days * 24 * 60 * 60)
    removed_file_count = 0
    removed_size_bytes = 0
    for path in _cleanup_roots(root):
        if path.is_file():
            file_count, size_bytes = _remove_old_file_if_needed(path, cutoff)
            removed_file_count += file_count
            removed_size_bytes += size_bytes
            continue
        if not path.is_dir():
            continue
        for entry in tuple(path.rglob("*")):
            if not entry.is_file():
                continue
            file_count, size_bytes = _remove_old_file_if_needed(entry, cutoff)
            removed_file_count += file_count
            removed_size_bytes += size_bytes
        _remove_empty_directories(path)
    return CacheCleanupResult(
        removed_file_count=removed_file_count,
        removed_size_bytes=removed_size_bytes,
    )


def category_open_path(category_id: str, cache_root: Path | None = None) -> Path:
    root = _cache_root(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    category = _category_by_id(category_id)
    if category.relative_paths:
        path = root / category.relative_paths[0]
        path.mkdir(parents=True, exist_ok=True)
        return path
    return root


def _cache_root(cache_root: Path | None) -> Path:
    return Path(cache_root) if cache_root is not None else app_cache_dir()


def _category_by_id(category_id: str) -> CacheCategory:
    for category in CACHE_CATEGORIES:
        if category.id == category_id:
            return category
    raise ValueError(f"未知缓存分类: {category_id}")


def _build_category_stats(
    root: Path,
    category: CacheCategory,
    covered_names: set[str],
) -> CacheCategoryStats:
    paths = _category_paths(root, category, covered_names)
    size_bytes, file_count = _count_paths(paths)
    return CacheCategoryStats(
        id=category.id,
        label=category.label,
        paths=tuple(paths),
        path_summary=_path_summary(root, category, tuple(paths)),
        description=category.description,
        size_bytes=size_bytes,
        file_count=file_count,
    )


def _category_paths(
    root: Path,
    category: CacheCategory,
    covered_names: set[str],
) -> tuple[Path, ...]:
    if category.id == "other":
        return tuple(
            child
            for child in _safe_iterdir(root)
            if child.name not in covered_names
        )
    return tuple(root / relative_path for relative_path in category.relative_paths)


def _path_summary(root: Path, category: CacheCategory, paths: tuple[Path, ...]) -> str:
    if category.id == "other":
        return "缓存根目录中未分类的文件和目录"
    labels: list[str] = []
    for path in paths:
        try:
            labels.append(str(path.relative_to(root)))
        except ValueError:
            labels.append(str(path))
    return ", ".join(labels)


def _count_paths(paths: tuple[Path, ...]) -> tuple[int, int]:
    total_size = 0
    total_files = 0
    for path in paths:
        if path.is_file():
            try:
                total_size += path.stat().st_size
                total_files += 1
            except OSError:
                continue
            continue
        if not path.is_dir():
            continue
        for entry in path.rglob("*"):
            try:
                if not entry.is_file():
                    continue
                total_size += entry.stat().st_size
                total_files += 1
            except OSError:
                continue
    return total_size, total_files


def _clear_directory_contents(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in _safe_iterdir(path):
        _remove_path(child)


def _safe_iterdir(path: Path) -> tuple[Path, ...]:
    try:
        return tuple(path.iterdir())
    except OSError:
        return ()


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _cleanup_roots(root: Path) -> tuple[Path, ...]:
    covered_names = {
        relative_path
        for category in CACHE_CATEGORIES
        for relative_path in category.relative_paths
    }
    category_roots = [
        root / relative_path
        for category in CACHE_CATEGORIES
        for relative_path in category.relative_paths
    ]
    other_roots = [
        child
        for child in _safe_iterdir(root)
        if child.name not in covered_names
    ]
    return tuple(category_roots + other_roots)


def _remove_old_file_if_needed(path: Path, cutoff: float) -> tuple[int, int]:
    try:
        stat_result = path.stat()
    except OSError:
        return 0, 0
    if stat_result.st_mtime >= cutoff:
        return 0, 0
    size_bytes = stat_result.st_size
    try:
        path.unlink()
    except OSError:
        return 0, 0
    return 1, size_bytes


def _remove_empty_directories(path: Path) -> None:
    if not path.is_dir():
        return
    directories = sorted(
        (entry for entry in path.rglob("*") if entry.is_dir()),
        key=lambda entry: len(entry.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            continue
