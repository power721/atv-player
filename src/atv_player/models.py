from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atv_player.danmaku.models import DanmakuSourceGroup


@dataclass(slots=True)
class AppConfig:
    base_url: str = "http://127.0.0.1:4567"
    username: str = ""
    token: str = ""
    vod_token: str = ""
    last_path: str = "/"
    last_active_window: str = "main"
    last_playback_source: str = "browse"
    last_playback_source_key: str = ""
    last_playback_mode: str = ""
    last_playback_path: str = ""
    last_playback_vod_id: str = ""
    last_playback_clicked_vod_id: str = ""
    last_player_paused: bool = False
    player_volume: int = 100
    player_muted: bool = False
    player_wide_mode: bool = False
    preferred_parse_key: str = ""
    preferred_danmaku_enabled: bool = True
    preferred_danmaku_line_count: int = 1
    preferred_danmaku_render_mode: str = "static"
    preferred_danmaku_color_mode: str = "source"
    preferred_danmaku_uniform_color: str = "#FFFFFF"
    preferred_danmaku_position_preset: str = "top"
    preferred_danmaku_scroll_speed: float = 1.0
    preferred_danmaku_font_size: int = 32
    main_window_geometry: bytes | None = None
    player_window_geometry: bytes | None = None
    player_main_splitter_state: bytes | None = None
    browse_content_splitter_state: bytes | None = None
    last_selected_tab: str = "douban"
    last_selected_category_tab: str = ""
    last_selected_category_id: str = ""


@dataclass(slots=True)
class ExternalSubtitleOption:
    name: str
    lang: str
    url: str
    format: str = ""
    source: str = ""


@dataclass(slots=True)
class ExternalSubtitleSelection:
    source: str
    option_url: str
    option_name: str = ""
    option_lang: str = ""
    option_format: str = ""


@dataclass(slots=True)
class PlaybackDetailAction:
    id: str
    label: str
    active: bool = False
    enabled: bool = True
    visible: bool = True
    tooltip: str = ""


@dataclass(slots=True)
class PlaybackDetailFieldAction:
    type: str
    value: str
    target: str = ""


@dataclass(slots=True)
class PlaybackDetailValuePart:
    label: str
    action: PlaybackDetailFieldAction | None = None


@dataclass(slots=True, init=False)
class PlaybackDetailField:
    label: str
    value_parts: list[PlaybackDetailValuePart]

    def __init__(
        self,
        label: str,
        value: str = "",
        value_parts: list[PlaybackDetailValuePart] | None = None,
    ) -> None:
        self.label = label
        if value_parts is not None:
            self.value_parts = list(value_parts)
            return
        normalized = str(value or "").strip()
        self.value_parts = [PlaybackDetailValuePart(label=normalized)] if normalized else []

    @property
    def value(self) -> str:
        return " / ".join(part.label for part in self.value_parts)


@dataclass(slots=True)
class PlayItem:
    title: str
    url: str
    original_url: str = ""
    video_cover_override: str = ""
    path: str = ""
    index: int = 0
    size: int = 0
    duration_seconds: int = 0
    vod_id: str = ""
    detail_actions: list[PlaybackDetailAction] = field(default_factory=list)
    detail_fields: list[PlaybackDetailField] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    external_subtitles: list[ExternalSubtitleOption] = field(default_factory=list)
    playback_qualities: list["VideoQualityOption"] = field(default_factory=list)
    selected_playback_quality_id: str = ""
    dash_video_id: str = ""
    play_source: str = ""
    media_title: str = ""
    parse_required: bool = False
    danmaku_title_only: bool = False
    danmaku_xml: str = ""
    danmaku_pending: bool = False
    danmaku_series_key: str = ""
    danmaku_search_title: str = ""
    danmaku_search_episode: str = ""
    danmaku_search_query: str = ""
    danmaku_search_provider: str = ""
    danmaku_status_text: str = ""
    danmaku_search_query_overridden: bool = False
    danmaku_candidates: list[DanmakuSourceGroup] = field(default_factory=list)
    selected_danmaku_url: str = ""
    selected_danmaku_provider: str = ""
    selected_danmaku_title: str = ""
    danmaku_error: str = ""


@dataclass(slots=True)
class VideoQualityOption:
    id: str
    label: str
    url: str = ""
    width: int = 0
    height: int = 0
    bandwidth: int = 0
    codecs: str = ""


@dataclass(slots=True)
class PlaybackLoadResult:
    replacement_playlist: list[PlayItem] = field(default_factory=list)
    replacement_start_index: int = 0


@dataclass(slots=True)
class CategoryFilterOption:
    name: str
    value: str


@dataclass(slots=True)
class CategoryFilter:
    key: str
    name: str
    options: list[CategoryFilterOption] = field(default_factory=list)


@dataclass(slots=True)
class DoubanCategory:
    type_id: str
    type_name: str
    filters: list[CategoryFilter] = field(default_factory=list)


@dataclass(slots=True)
class VodItem:
    vod_id: str
    vod_name: str
    detail_style: str = ""
    path: str = ""
    share_type: str = ""
    vod_pic: str = ""
    vod_tag: str = ""
    vod_time: str = ""
    vod_remarks: str = ""
    vod_play_from: str = ""
    vod_play_url: str = ""
    type_name: str = ""
    vod_content: str = ""
    vod_year: str = ""
    vod_area: str = ""
    vod_lang: str = ""
    vod_director: str = ""
    vod_actor: str = ""
    epg_current: str = ""
    epg_schedule: str = ""
    dbid: int = 0
    type: int = 0
    detail_fields: list[PlaybackDetailField] = field(default_factory=list)
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
    playlist_index: int = 0
    source_kind: str = "remote"
    source_plugin_id: int = 0
    source_plugin_name: str = ""
    source_key: str = ""
    source_name: str = ""


@dataclass(slots=True)
class LiveSourceConfig:
    id: int = 0
    source_type: str = ""
    source_value: str = ""
    display_name: str = ""
    enabled: bool = True
    sort_order: int = 0
    is_default: bool = False
    last_refreshed_at: int = 0
    last_error: str = ""
    cache_text: str = ""


@dataclass(slots=True)
class LiveSourceEntry:
    id: int = 0
    source_id: int = 0
    group_name: str = ""
    channel_name: str = ""
    stream_url: str = ""
    logo_url: str = ""
    sort_order: int = 0


@dataclass(slots=True)
class LiveEpgConfig:
    id: int = 1
    epg_url: str = ""
    cache_text: str = ""
    last_refreshed_at: int = 0
    last_error: str = ""


@dataclass(slots=True)
class LiveSourceChannelView:
    source_id: int
    channel_id: str
    group_key: str
    channel_name: str
    stream_url: str
    logo_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SpiderPluginConfig:
    id: int = 0
    source_type: str = ""
    source_value: str = ""
    display_name: str = ""
    enabled: bool = True
    sort_order: int = 0
    cached_file_path: str = ""
    last_loaded_at: int = 0
    last_error: str = ""
    config_text: str = ""
    plugin_version: int = 1


@dataclass(slots=True)
class SpiderPluginImportProgress:
    stage: str
    current: int = 0
    total: int = 0
    message: str = ""


@dataclass(slots=True)
class SpiderPluginImportResult:
    imported_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0


class SpiderPluginImportCancelled(Exception):
    def __init__(self, result: SpiderPluginImportResult) -> None:
        super().__init__("已取消导入")
        self.result = result


@dataclass(slots=True)
class SpiderPluginLogEntry:
    id: int = 0
    plugin_id: int = 0
    level: str = "info"
    message: str = ""
    created_at: int = 0


@dataclass(slots=True)
class SpiderPluginAction:
    id: str
    label: str
    enabled: bool = True
    visible: bool = True
    tooltip: str = ""


@dataclass(slots=True)
class SpiderPluginActionContext:
    parent: object | None
    plugin_id: int
    plugin_name: str
    config_text: str
    set_config_text: Callable[[str], None]
    refresh_plugin: Callable[[], None]
    log: Callable[[str, str], None]


@dataclass(slots=True)
class OpenPlayerRequest:
    vod: VodItem
    playlist: list[PlayItem]
    clicked_index: int
    playlists: list[list[PlayItem]] = field(default_factory=list)
    playlist_index: int = 0
    source_kind: str = "browse"
    source_key: str = ""
    source_mode: str = ""
    source_path: str = ""
    source_vod_id: str = ""
    source_clicked_vod_id: str = ""
    detail_resolver: Callable[[PlayItem], VodItem | None] | None = None
    resolved_vod_by_id: dict[str, VodItem] = field(default_factory=dict)
    use_local_history: bool = True
    restore_history: bool = False
    playback_loader: Callable[..., PlaybackLoadResult | None] | None = None
    async_playback_loader: bool = False
    detail_action_runner: Callable[[PlayItem, str], list[PlaybackDetailAction]] | None = None
    detail_field_runner: Callable[[PlayItem, PlaybackDetailFieldAction], None] | None = None
    danmaku_controller: object | None = None
    playback_progress_reporter: Callable[[PlayItem, int, bool], None] | None = None
    playback_stopper: Callable[[PlayItem], None] | None = None
    playback_history_loader: Callable[[], HistoryRecord | None] | None = None
    playback_history_saver: Callable[[dict[str, object]], None] | None = None
    initial_log_message: str = ""
    is_placeholder: bool = False
