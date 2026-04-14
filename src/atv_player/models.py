from dataclasses import dataclass, field


@dataclass(slots=True)
class AppConfig:
    base_url: str = "http://127.0.0.1:4567"
    username: str = ""
    token: str = ""
    vod_token: str = ""
    last_path: str = "/"
    last_active_window: str = "main"
    last_playback_mode: str = ""
    last_playback_path: str = ""
    last_playback_vod_id: str = ""
    last_playback_clicked_vod_id: str = ""
    main_window_geometry: bytes | None = None
    player_window_geometry: bytes | None = None


@dataclass(slots=True)
class PlayItem:
    title: str
    url: str
    path: str = ""
    index: int = 0
    size: int = 0


@dataclass(slots=True)
class VodItem:
    vod_id: str
    vod_name: str
    path: str = ""
    vod_pic: str = ""
    vod_tag: str = ""
    vod_time: str = ""
    vod_remarks: str = ""
    vod_play_from: str = ""
    vod_play_url: str = ""
    type_name: str = ""
    vod_content: str = ""
    dbid: int = 0
    type: int = 0
    items: list[PlayItem] = field(default_factory=list)


@dataclass(slots=True)
class HistoryRecord:
    id: int
    key: str
    vod_name: str
    vod_pic: str
    vod_remarks: str
    episode: int
    episode_url: str
    position: int
    opening: int
    ending: int
    speed: float
    create_time: int


@dataclass(slots=True)
class OpenPlayerRequest:
    vod: VodItem
    playlist: list[PlayItem]
    clicked_index: int
    source_mode: str = ""
    source_path: str = ""
    source_vod_id: str = ""
    source_clicked_vod_id: str = ""
