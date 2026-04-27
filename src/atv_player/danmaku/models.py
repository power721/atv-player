from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DanmakuSearchItem:
    provider: str
    name: str
    url: str
    ratio: float = 0.0
    simi: float = 0.0


@dataclass(frozen=True, slots=True)
class DanmakuRecord:
    time_offset: float
    pos: int
    color: str
    content: str
