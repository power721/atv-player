from __future__ import annotations

from typing import Protocol

from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem


class DanmakuProvider(Protocol):
    key: str

    def search(self, name: str) -> list[DanmakuSearchItem]: ...

    def resolve(self, page_url: str) -> list[DanmakuRecord]: ...

    def supports(self, page_url: str) -> bool: ...
