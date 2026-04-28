from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DanmakuSearchItem:
    provider: str
    name: str
    url: str
    ratio: float = 0.0
    simi: float = 0.0
    cid: int | None = None
    bvid: str = ""
    aid: int | None = None
    ep_id: int | None = None
    season_id: int | None = None
    search_type: str = ""


@dataclass(frozen=True, slots=True)
class DanmakuRecord:
    time_offset: float
    pos: int
    color: str
    content: str
