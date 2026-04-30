from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DanmakuSearchItem:
    provider: str
    name: str
    url: str
    ratio: float = 0.0
    simi: float = 0.0
    duration_seconds: int = 0
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


@dataclass(frozen=True, slots=True)
class DanmakuSourceOption:
    provider: str
    name: str
    url: str
    ratio: float = 0.0
    simi: float = 0.0
    duration_seconds: int = 0
    episode_match: bool = False
    preferred_by_history: bool = False
    resolve_ready: bool = True


@dataclass(frozen=True, slots=True)
class DanmakuSourceGroup:
    provider: str
    provider_label: str
    options: list[DanmakuSourceOption]
    preferred_by_history: bool = False


@dataclass(frozen=True, slots=True)
class DanmakuSourceSearchResult:
    groups: list[DanmakuSourceGroup]
    default_option_url: str = ""
    default_provider: str = ""


@dataclass(frozen=True, slots=True)
class DanmakuSeriesPreference:
    series_key: str
    provider: str
    page_url: str
    title: str
    search_title: str = ""
    updated_at: int = 0
