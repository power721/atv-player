from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon

_ICON_CACHE: dict[str, QIcon] = {}


def load_icon(path: str | Path) -> QIcon:
    key = str(path)
    icon = _ICON_CACHE.get(key)
    if icon is None:
        icon = QIcon(key)
        _ICON_CACHE[key] = icon
    return icon


def clear_icon_cache() -> None:
    _ICON_CACHE.clear()
