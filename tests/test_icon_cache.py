from pathlib import Path

from PySide6.QtGui import QIcon

import atv_player.ui.icon_cache as icon_cache_module


def test_load_icon_caches_qicon_instances(monkeypatch) -> None:
    icon_cache_module.clear_icon_cache()
    calls: list[str] = []

    class RecordingIcon(QIcon):
        def __init__(self, path: str) -> None:
            calls.append(path)
            super().__init__()

    monkeypatch.setattr(icon_cache_module, "QIcon", RecordingIcon)

    first = icon_cache_module.load_icon(Path("/tmp/icon.svg"))
    second = icon_cache_module.load_icon("/tmp/icon.svg")

    assert first is second
    assert calls == ["/tmp/icon.svg"]
