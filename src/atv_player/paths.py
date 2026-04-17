from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths

APP_NAME = "atv-player"


def _writable_location(
    location: QStandardPaths.StandardLocation,
    fallback: Path,
) -> Path:
    resolved = QStandardPaths.writableLocation(location)
    if resolved:
        return Path(resolved)
    return fallback


def app_data_dir() -> Path:
    path = _writable_location(
        QStandardPaths.StandardLocation.AppDataLocation,
        Path.home() / ".local" / "share" / APP_NAME,
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def app_cache_dir() -> Path:
    path = _writable_location(
        QStandardPaths.StandardLocation.CacheLocation,
        Path.home() / ".cache" / APP_NAME,
    )
    path.mkdir(parents=True, exist_ok=True)
    return path
