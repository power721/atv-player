import base64
import logging
import inspect
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from time import time
from typing import cast
from urllib.parse import parse_qs, urlparse

from atv_player.models import (
    HistoryRecord,
    PlayItem,
    PlaybackSource,
    PlaybackSourceGroup,
    PlaybackDetailAction,
    PlaybackDetailField,
    PlaybackDetailFieldAction,
    PlaybackLoadResult,
    VodItem,
)
from atv_player.episode_titles import playlist_item_display_title
from atv_player.player.resume import resolve_resume_index, resolve_resume_index_by_drive_path
from atv_player.yt_dlp_service import looks_like_youtube_video_id


logger = logging.getLogger(__name__)


def split_drive_path(path: str) -> tuple[str, str]:
    """网盘 AList 完整路径 → (分享身份, 资源内相对路径)。

    分享挂载段为 ``/temp/<盘类型>@<分享ID>@<提取码>/...``(见后端 ShareService.add);
    返回 ("", "") 表示不是网盘分享路径。
    """
    value = str(path or "").strip()
    marker = "/temp/"
    index = value.find(marker)
    if index < 0:
        return "", ""
    rest = value[index + len(marker):]
    slash = rest.find("/")
    if slash <= 0:
        return "", ""
    return rest[:slash], rest[slash:]


def decode_drive_dir_id(value: str) -> str:
    """drive_dir_id 是目录绝对路径的 base64url 编码(见后端 DriveService),解不出返回空。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        padded = text + "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


@dataclass(slots=True)
class PlayerSession:
    vod: VodItem
    playlist: list[PlayItem]
    start_index: int
    start_position_seconds: int
    speed: float
    playlists: list[list[PlayItem]] = field(default_factory=list)
    playlist_index: int = 0
    source_groups: list[PlaybackSourceGroup] = field(default_factory=list)
    source_group_index: int = 0
    source_index: int = 0
    opening_seconds: int = 0
    ending_seconds: int = 0
    detail_resolver: Callable[[PlayItem], VodItem | None] | None = None
    resolved_vod_by_id: dict[str, VodItem] = field(default_factory=dict)
    use_local_history: bool = True
    playback_loader: Callable[[PlayItem], PlaybackLoadResult | None] | None = None
    async_playback_loader: bool = False
    detail_action_runner: Callable[[PlayItem, str], list[PlaybackDetailAction]] | None = None
    detail_field_runner: Callable[[PlayItem, PlaybackDetailFieldAction], None] | None = None
    metadata_hydrator: Callable[[object], VodItem | None] | None = None
    metadata_scrape_service: object | None = None
    subtitle_search_service: object | None = None
    metadata_binding_repository: object | None = None
    episode_title_override_repository: object | None = None
    metadata_hydrated: bool = False
    episode_title_enhancer: Callable[[object], list[PlayItem] | None] | None = None
    episode_titles_hydrated: bool = False
    # 手动"重写剧集标题"时置位：跳过已保存的标题缓存，强制重新搜索
    episode_titles_force_refresh: bool = False
    original_vod: VodItem | None = None
    show_original_metadata: bool = False
    current_metadata_poster_index: int = 0
    original_item_detail_fields_by_key: dict[tuple[str, str, str, str, str], list[PlaybackDetailField]] = field(
        default_factory=dict
    )
    danmaku_controller: object | None = None
    playback_progress_reporter: Callable[[PlayItem, int, bool], None] | None = None
    playback_stopper: Callable[[PlayItem], None] | None = None
    playback_history_saver: Callable[[dict[str, object]], None] | None = None
    initial_log_message: str = ""
    initial_vod_name: str = ""
    is_placeholder: bool = False
    source_kind: str = ""
    source_key: str = ""
    source_display_name: str = ""
    video_cover_override: str = ""
    prefetched_next_danmaku_indices: set[int] = field(default_factory=set)
    pending_next_danmaku_prefetch_token: int = 0
    # New per-directory drive API state for lazy directory loading.
    drive_resource_id: str = ""
    drive_files_loader: Callable[..., list[PlayItem]] | None = None
    # Retained until a lazily resolved drive directory can match its saved media URL.
    # Nested drive directories do not have their own source indexes in history.
    resume_history: HistoryRecord | None = None


class PlayerController:
    _NEXT_EPISODE_DANMAKU_PREFETCH_DELAY_SECONDS = 10.0

    def __init__(self, api_client) -> None:
        self._api_client = api_client
        self._prefetch_timer_factory = lambda delay_seconds, callback: threading.Timer(delay_seconds, callback)

    def _bind_playback_loader(
        self,
        playback_loader: Callable[..., PlaybackLoadResult | None] | None,
        session: PlayerSession,
    ) -> Callable[[PlayItem], PlaybackLoadResult | None] | None:
        if playback_loader is None:
            return None
        parameters = list(inspect.signature(playback_loader).parameters.values())
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters)
        if has_varargs or len(positional) >= 2:
            return lambda item, playback_loader=playback_loader, session=session: playback_loader(session, item)
        return cast(Callable[[PlayItem], PlaybackLoadResult | None], playback_loader)

    def _is_placeholder_history_title(self, title: str) -> bool:
        normalized = str(title or "").strip()
        if not normalized:
            return True
        if normalized in {"解析播放", "待解析"}:
            return True
        lowered = normalized.lower()
        return looks_like_youtube_video_id(normalized) or lowered.startswith(("http://", "https://", "yt:video:"))

    def _is_youtube_play_item(self, item: PlayItem) -> bool:
        for value in (item.original_url, item.vod_id, item.url):
            normalized = str(value or "").strip()
            lowered = normalized.lower()
            if looks_like_youtube_video_id(normalized):
                return True
            if lowered.startswith(("yt:video:", "yt:channel:", "yt:playlist:")):
                return True
            if "youtube.com" in lowered or "youtu.be" in lowered:
                return True
        return False

    def _is_ytdlp_play_item(self, item: PlayItem) -> bool:
        if str(item.selected_playback_quality_id or "").startswith("ytdlp_"):
            return True
        if str(item.selected_audio_track_id or "").startswith("ytdlp_audio_"):
            return True
        if str(item.ytdl_format or "").strip():
            return True
        return any(str(quality.id or "").startswith("ytdlp_") for quality in item.playback_qualities)

    def _googlevideo_expire_seconds(self, url: str) -> int:
        parsed = urlparse(str(url or "").strip())
        values = parse_qs(parsed.query).get("expire") or []
        for value in values:
            if str(value).isdigit():
                return int(value)
        parts = [part for part in parsed.path.split("/") if part]
        for index, part in enumerate(parts[:-1]):
            if part == "expire" and parts[index + 1].isdigit():
                return int(parts[index + 1])
        return 0

    def _is_googlevideo_videoplayback_url(self, url: str) -> bool:
        parsed = urlparse(str(url or "").strip())
        hostname = (parsed.hostname or "").lower()
        if hostname != "googlevideo.com" and not hostname.endswith(".googlevideo.com"):
            return False
        return any(part == "videoplayback" for part in parsed.path.split("/") if part)

    def _is_reusable_youtube_history_url(self, url: str) -> bool:
        parsed = urlparse(str(url or "").strip())
        hostname = (parsed.hostname or "").lower()
        if hostname != "googlevideo.com" and not hostname.endswith(".googlevideo.com"):
            return False
        if self._is_googlevideo_videoplayback_url(url):
            return False
        expire_seconds = self._googlevideo_expire_seconds(url)
        return expire_seconds > int(time()) + 120

    def _should_skip_history_report_for_youtube_placeholder(
        self,
        session: PlayerSession,
        current_item: PlayItem,
    ) -> bool:
        if session.playback_loader is None or len(session.playlist) != 1:
            return False
        normalized_vod_id = str(current_item.vod_id or "").strip().lower()
        return normalized_vod_id.startswith(("yt:channel:", "yt:playlist:"))

    def _history_episode_url(
        self,
        current_item: PlayItem,
        session: PlayerSession | None = None,
    ) -> str:
        play_id = str(current_item.play_id or "").strip()
        if play_id:
            subgroup_index = (
                self._history_source_subgroup_index(session)
                if session is not None
                else 0
            )
            episode_index = max(0, int(current_item.index))
            return f"{play_id}@{subgroup_index}@{episode_index}"
        url = str(current_item.url or "").strip()
        original_url = str(current_item.original_url or "").strip()
        if original_url and self._is_youtube_play_item(current_item):
            if current_item.audio_url:
                return original_url
            if self._is_googlevideo_videoplayback_url(url):
                return original_url
        return url

    def _prefill_reusable_history_url(
        self,
        playlist: list[PlayItem],
        start_index: int,
        history_episode_url: str,
    ) -> None:
        if not (0 <= start_index < len(playlist)):
            return
        current_item = playlist[start_index]
        if not self._is_youtube_play_item(current_item):
            logger.info(
                "Skip reusable YouTube history url prefill reason=not_youtube start_index=%s history_url_host=%s",
                start_index,
                urlparse(history_episode_url).hostname or "",
            )
            return
        if not self._is_reusable_youtube_history_url(history_episode_url):
            logger.info(
                "Skip reusable YouTube history url prefill reason=not_reusable start_index=%s history_url_host=%s expire=%s",
                start_index,
                urlparse(history_episode_url).hostname or "",
                self._googlevideo_expire_seconds(history_episode_url),
            )
            return
        if not current_item.original_url:
            current_item.original_url = current_item.url or current_item.vod_id
        current_item.url = history_episode_url
        logger.info(
            "Prefilled reusable YouTube history url start_index=%s original_url=%s history_url_host=%s expire=%s",
            start_index,
            current_item.original_url,
            urlparse(history_episode_url).hostname or "",
            self._googlevideo_expire_seconds(history_episode_url),
        )

    def _detail_field_value(self, fields: list[PlaybackDetailField], label: str) -> str:
        for field in fields:
            if str(field.label or "").strip() == label:
                value = str(field.value or "").strip()
                if value:
                    return value
        return ""

    def _youtube_channel_detail_name(self, session: PlayerSession, current_item: PlayItem) -> str:
        return self._detail_field_value(session.vod.detail_fields, "频道") or self._detail_field_value(
            current_item.detail_fields,
            "频道",
        )

    def _looks_like_youtube_channel_identifier(self, title: str, session: PlayerSession) -> bool:
        normalized = str(title or "").strip()
        if not normalized:
            return False
        if normalized == str(session.vod.vod_id or "").strip():
            return True
        return normalized.startswith("UC") or normalized.startswith("@")

    def _history_vod_name(self, session: PlayerSession, current_item: PlayItem) -> str:
        initial_vod_name = str(session.initial_vod_name or "").strip()
        is_ytdlp_item = self._is_youtube_play_item(current_item) or self._is_ytdlp_play_item(current_item)
        channel_detail_name = self._youtube_channel_detail_name(session, current_item) if is_ytdlp_item else ""
        if (
            initial_vod_name
            and channel_detail_name
            and initial_vod_name != channel_detail_name
            and not self._is_placeholder_history_title(initial_vod_name)
            and self._looks_like_youtube_channel_identifier(initial_vod_name, session)
        ):
            return channel_detail_name
        if (
            initial_vod_name
            and initial_vod_name != str(session.vod.vod_name or "").strip()
            and not self._is_placeholder_history_title(initial_vod_name)
            and is_ytdlp_item
        ):
            return initial_vod_name
        return session.vod.vod_name

    def _build_legacy_source_groups(
        self,
        playlist: list[PlayItem],
        playlists: list[list[PlayItem]] | None,
    ) -> list[PlaybackSourceGroup]:
        normalized = [group for group in (playlists or []) if group]
        if not normalized:
            normalized = [playlist]
        source_groups: list[PlaybackSourceGroup] = []
        for group_index, current_playlist in enumerate(normalized):
            label = (
                current_playlist[0].play_source
                if current_playlist and current_playlist[0].play_source
                else f"线路 {group_index + 1}"
            )
            source_groups.append(
                PlaybackSourceGroup(
                    label=label,
                    sources=[PlaybackSource(label=label, playlist=current_playlist)],
                )
            )
        return source_groups

    def _flatten_source_groups(
        self,
        source_groups: list[PlaybackSourceGroup],
    ) -> tuple[list[list[PlayItem]], dict[tuple[int, int], int], dict[int, tuple[int, int]]]:
        playlists: list[list[PlayItem]] = []
        pair_to_flat: dict[tuple[int, int], int] = {}
        flat_to_pair: dict[int, tuple[int, int]] = {}
        for group_index, group in enumerate(source_groups):
            for source_index, source in enumerate(group.sources):
                flat_index = len(playlists)
                playlists.append(source.playlist)
                pair_to_flat[(group_index, source_index)] = flat_index
                flat_to_pair[flat_index] = (group_index, source_index)
        return playlists, pair_to_flat, flat_to_pair

    def _normalize_source_groups(
        self,
        playlist: list[PlayItem],
        playlists: list[list[PlayItem]] | None,
        playlist_index: int,
        source_groups: list[PlaybackSourceGroup] | None,
        source_group_index: int,
        source_index: int,
    ) -> tuple[list[PlaybackSourceGroup], list[list[PlayItem]], int, int, int, list[PlayItem]]:
        normalized_groups = [group for group in (source_groups or []) if group.sources]
        if not normalized_groups:
            normalized_groups = self._build_legacy_source_groups(playlist, playlists)
            source_group_index = max(0, min(playlist_index, len(normalized_groups) - 1))
            source_index = 0
        flat_playlists, pair_to_flat, _ = self._flatten_source_groups(normalized_groups)
        if not flat_playlists:
            flat_playlists = [playlist]
            normalized_groups = [
                PlaybackSourceGroup(label="线路 1", sources=[PlaybackSource(label="线路 1", playlist=playlist)])
            ]
            pair_to_flat = {(0, 0): 0}
        source_group_index = max(0, min(source_group_index, len(normalized_groups) - 1))
        active_group = normalized_groups[source_group_index]
        source_index = max(0, min(source_index, len(active_group.sources) - 1))
        playlist_index = pair_to_flat[(source_group_index, source_index)]
        return (
            normalized_groups,
            flat_playlists,
            playlist_index,
            source_group_index,
            source_index,
            active_group.sources[source_index].playlist,
        )

    def _restore_selected_source(
        self,
        source_groups: list[PlaybackSourceGroup],
        playlists: list[list[PlayItem]],
        playlist_index: int,
        source_group_index: int,
        source_index: int,
        history: HistoryRecord | None,
    ) -> tuple[int, int, int, list[PlayItem]]:
        _, pair_to_flat, flat_to_pair = self._flatten_source_groups(source_groups)
        if history is not None:
            # 跨端记录优先按规范分享身份定位网盘资源:安卓端不产生分组/资源坐标
            # (本地缺省为 0,会误选第一个分组),分享 ID 才是资源级稳定指针。
            share_pair = self._match_source_by_drive_share_key(
                source_groups, history.drive_share_key)
            if share_pair is not None:
                source_group_index, source_index = share_pair
            else:
                history_pair = (history.source_group_index, history.source_index)
                pair_flat_index = pair_to_flat.get(history_pair, -1)
                should_use_explicit_pair = (
                    history_pair in pair_to_flat
                    and (
                        history.source_group_index != 0
                        or history.source_index != 0
                        or pair_flat_index == history.playlist_index
                    )
                )
                if should_use_explicit_pair:
                    source_group_index = history.source_group_index
                    active_group = source_groups[source_group_index]
                    if 0 <= history.source_index < len(active_group.sources):
                        source_index = history.source_index
                    else:
                        source_index = 0
                elif 0 <= history.playlist_index < len(playlists):
                    source_group_index, source_index = flat_to_pair[history.playlist_index]
        playlist_index = pair_to_flat[(source_group_index, source_index)]
        return (
            source_group_index,
            source_index,
            playlist_index,
            source_groups[source_group_index].sources[source_index].playlist,
        )

    @staticmethod
    def _match_source_by_drive_share_key(
        source_groups: list[PlaybackSourceGroup],
        share_key: str,
    ) -> tuple[int, int] | None:
        """按 driveShareKey(盘类型@分享ID@提取码)在源列表中定位网盘资源,找不到返回 None。"""
        parts = str(share_key or "").split("@")
        if len(parts) < 2 or not parts[1]:
            return None
        share_id = parts[1]
        for group_index, group in enumerate(source_groups):
            for source_index, source in enumerate(group.sources):
                for item in source.playlist:
                    for candidate in (item.vod_id, item.url):
                        if candidate and share_id in str(candidate):
                            return group_index, source_index
        return None

    def create_session(
        self,
        vod: VodItem,
        playlist: list[PlayItem],
        clicked_index: int,
        playlists: list[list[PlayItem]] | None = None,
        playlist_index: int = 0,
        source_groups: list[PlaybackSourceGroup] | None = None,
        source_group_index: int = 0,
        source_index: int = 0,
        source_kind: str = "",
        source_key: str = "",
        source_display_name: str = "",
        detail_resolver: Callable[[PlayItem], VodItem | None] | None = None,
        resolved_vod_by_id: dict[str, VodItem] | None = None,
        use_local_history: bool = True,
        restore_history: bool = False,
        playback_loader: Callable[[PlayItem], PlaybackLoadResult | None] | None = None,
        async_playback_loader: bool = False,
        detail_action_runner: Callable[[PlayItem, str], list[PlaybackDetailAction]] | None = None,
        detail_field_runner: Callable[[PlayItem, PlaybackDetailFieldAction], None] | None = None,
        metadata_hydrator: Callable[[object], VodItem | None] | None = None,
        metadata_scrape_service: object | None = None,
        subtitle_search_service: object | None = None,
        metadata_binding_repository: object | None = None,
        episode_title_override_repository: object | None = None,
        episode_title_enhancer: Callable[[object], list[PlayItem] | None] | None = None,
        danmaku_controller: object | None = None,
        playback_progress_reporter: Callable[[PlayItem, int, bool], None] | None = None,
        playback_stopper: Callable[[PlayItem], None] | None = None,
        playback_history_loader: Callable[[], HistoryRecord | None] | None = None,
        playback_history_saver: Callable[[dict[str, object]], None] | None = None,
        initial_log_message: str = "",
        is_placeholder: bool = False,
        drive_resource_id: str = "",
        drive_files_loader: Callable[..., list[PlayItem]] | None = None,
    ) -> PlayerSession:
        normalized_source_groups, normalized_playlists, playlist_index, source_group_index, source_index, active_playlist = self._normalize_source_groups(
            playlist,
            playlists,
            playlist_index,
            source_groups,
            source_group_index,
            source_index,
        )
        history = playback_history_loader() if playback_history_loader is not None else None
        if history is None and (use_local_history or restore_history):
            history = self._api_client.get_history(vod.vod_id)
        source_group_index, source_index, playlist_index, active_playlist = self._restore_selected_source(
            normalized_source_groups,
            normalized_playlists,
            playlist_index,
            source_group_index,
            source_index,
            history,
        )
        start_index = resolve_resume_index(history, active_playlist, clicked_index)
        history_episode = history.episode if history is not None else None
        history_episode_url = history.episode_url if history is not None else ""
        drive_path_index = resolve_resume_index_by_drive_path(history, active_playlist)
        matched_history = history is not None and (
            start_index == history.episode
            or (drive_path_index is not None and start_index == drive_path_index)
            or playback_history_loader is not None
        )
        if matched_history and history is not None:
            position_seconds = int(history.position / 1000)
            speed = history.speed
        else:
            position_seconds = 0
            speed = 1.0
        if matched_history and history_episode_url:
            self._prefill_reusable_history_url(active_playlist, start_index, history_episode_url)
        logger.info(
            (
                "Create player session vod_id=%s playlist_size=%s clicked_index=%s "
                "start_index=%s restored=%s playlist_index=%s source_group_index=%s source_index=%s "
                "history_episode=%s history_episode_url=%s"
            ),
            vod.vod_id,
            len(active_playlist),
            clicked_index,
            start_index,
            matched_history,
            playlist_index,
            source_group_index,
            source_index,
            history_episode,
            history_episode_url,
        )
        session = PlayerSession(
            vod=vod,
            playlist=active_playlist,
            start_index=start_index,
            start_position_seconds=position_seconds,
            speed=speed,
            source_kind=source_kind,
            source_key=source_key,
            source_display_name=source_display_name,
            playlists=normalized_playlists,
            playlist_index=playlist_index,
            source_groups=normalized_source_groups,
            source_group_index=source_group_index,
            source_index=source_index,
            opening_seconds=int((history.opening if history else 0) / 1000),
            ending_seconds=int((history.ending if history else 0) / 1000),
            detail_resolver=detail_resolver,
            resolved_vod_by_id=dict(resolved_vod_by_id or {}),
            use_local_history=use_local_history,
            playback_loader=playback_loader,
            async_playback_loader=async_playback_loader,
            detail_action_runner=detail_action_runner,
            detail_field_runner=detail_field_runner,
            metadata_hydrator=metadata_hydrator,
            metadata_scrape_service=metadata_scrape_service,
            subtitle_search_service=subtitle_search_service,
            metadata_binding_repository=metadata_binding_repository,
            episode_title_override_repository=episode_title_override_repository,
            episode_title_enhancer=episode_title_enhancer,
            danmaku_controller=danmaku_controller,
            playback_progress_reporter=playback_progress_reporter,
            playback_stopper=playback_stopper,
            initial_log_message=initial_log_message,
            initial_vod_name=str(vod.vod_name or ""),
            is_placeholder=is_placeholder,
            drive_resource_id=drive_resource_id,
            drive_files_loader=drive_files_loader,
            resume_history=history,
        )
        session.playback_loader = self._bind_playback_loader(playback_loader, session)
        session.playback_history_saver = playback_history_saver
        return session

    def resolve_play_item_detail(self, session: PlayerSession, play_item: PlayItem) -> VodItem | None:
        if not play_item.vod_id or session.detail_resolver is None:
            return None
        if play_item.vod_id in session.resolved_vod_by_id:
            resolved_vod = session.resolved_vod_by_id[play_item.vod_id]
            if resolved_vod is None:
                return None
        else:
            resolved_vod = session.detail_resolver(play_item)
            session.resolved_vod_by_id[play_item.vod_id] = resolved_vod
        if resolved_vod is None:
            return None
        url = resolved_vod.items[0].url if resolved_vod.items else resolved_vod.vod_play_url
        if not url:
            return None
        play_item.url = url
        # 浏览/网盘流程:外挂字幕随详情一起解析(直链带时效),跟着播放地址一起落到播放项。
        if resolved_vod.items and resolved_vod.items[0].external_subtitles and not play_item.external_subtitles:
            play_item.external_subtitles = list(resolved_vod.items[0].external_subtitles)
        return resolved_vod

    def report_progress(
        self,
        session: PlayerSession,
        current_index: int,
        position_seconds: int,
        speed: float,
        opening_seconds: int,
        ending_seconds: int,
        paused: bool,
        force_remote_report: bool = False,
        duration_seconds: int = 0,
    ) -> None:
        if not (0 <= current_index < len(session.playlist)):
            return
        current_item = session.playlist[current_index]
        position_ms = position_seconds * 1000
        if session.playback_progress_reporter is not None and (not paused or force_remote_report):
            session.playback_progress_reporter(current_item, position_ms, paused)
        if self._should_skip_history_report_for_youtube_placeholder(session, current_item):
            logger.info(
                "Skip playback history report for unresolved YouTube placeholder vod_id=%s index=%s position_ms=%s",
                session.vod.vod_id,
                current_index,
                position_ms,
            )
            return
        logger.info(
            "Report playback progress vod_id=%s index=%s position_ms=%s paused=%s",
            session.vod.vod_id,
            current_index,
            position_ms,
            paused,
        )
        drive_share_key, drive_path = self._history_drive_ref(session, current_item)
        payload = {
            "cid": 0,
            "key": session.vod.vod_id,
            "vodName": self._history_vod_name(session, current_item),
            "vodPic": session.vod.vod_pic,
            "vodRemarks": playlist_item_display_title(current_item, "episode"),
            "episode": current_index,
            "episodeUrl": self._history_episode_url(current_item, session),
            "position": position_ms,
            "duration": duration_seconds * 1000,
            "opening": opening_seconds * 1000,
            "ending": ending_seconds * 1000,
            "speed": speed,
            "playlistIndex": session.playlist_index,
            "sourceGroupIndex": session.source_group_index,
            "sourceIndex": session.source_index,
            "sourceSubgroupIndex": self._history_source_subgroup_index(session),
            "sourceSubgroupName": self._history_source_subgroup_name(session),
            "driveDirId": self._history_drive_dir_id(session),
            "driveShareKey": drive_share_key,
            "drivePath": drive_path,
            "createTime": int(time() * 1000),
        }
        if session.playback_history_saver is not None:
            session.playback_history_saver(payload)
        if (
            not paused
            and duration_seconds > 15 * 60
            and (duration_seconds - position_seconds) < 150
        ):
            self._schedule_next_episode_danmaku_prefetch(session, current_index)
        # 后端已下线 /api/history(POST 持久化)。本地 playback_history_saver 已写入
        # media_playback_history,多端上报改由 PlaybackHistorySyncService PUSH 负责。

    @staticmethod
    def _history_drive_subgroup(session: PlayerSession) -> PlaybackSourceGroup | None:
        if not (0 <= session.source_group_index < len(session.source_groups)):
            return None
        group = session.source_groups[session.source_group_index]
        if not (0 <= session.source_index < len(group.sources)):
            return None
        source = group.sources[session.source_index]
        if not source.subgroups or not (0 <= source.subgroup_index < len(source.subgroups)):
            return None
        return source.subgroups[source.subgroup_index]

    def _history_source_subgroup_index(self, session: PlayerSession | None) -> int:
        if session is None:
            return 0
        subgroup = self._history_drive_subgroup(session)
        if subgroup is None:
            return 0
        group = session.source_groups[session.source_group_index]
        return group.sources[session.source_index].subgroup_index

    def _history_source_subgroup_name(self, session: PlayerSession) -> str:
        subgroup = self._history_drive_subgroup(session)
        return subgroup.label if subgroup is not None else ""

    def _history_drive_dir_id(self, session: PlayerSession) -> str:
        subgroup = self._history_drive_subgroup(session)
        return subgroup.drive_dir_id if subgroup is not None else ""

    def _history_drive_ref(
        self,
        session: PlayerSession | None,
        current_item: PlayItem,
    ) -> tuple[str, str]:
        """当前网盘文件 → 规范标识 (driveShareKey, drivePath),与后端归一化格式对齐。

        优先用 PlayItem.path(/api/drive 返回的文件完整路径);拿不到时退回
        子目录 id(目录绝对路径)+ 文件名拼接。非网盘播放返回 ("", "")。
        """
        share_key, rel_path = split_drive_path(current_item.path)
        if share_key:
            return share_key, rel_path
        subgroup = self._history_drive_subgroup(session) if session is not None else None
        if subgroup is not None and subgroup.drive_dir_id:
            dir_share_key, dir_path = split_drive_path(decode_drive_dir_id(subgroup.drive_dir_id))
            name = str(current_item.original_title or current_item.title or "").strip()
            if dir_share_key and name:
                return dir_share_key, dir_path + "/" + name
        return "", ""

    def stop_playback(self, session: PlayerSession, current_index: int) -> None:
        self._invalidate_pending_next_episode_danmaku_prefetch(session)
        if session.playback_stopper is None:
            return
        if not (0 <= current_index < len(session.playlist)):
            return
        logger.info("Stop playback vod_id=%s index=%s", session.vod.vod_id, current_index)
        session.playback_stopper(session.playlist[current_index])

    def reset_next_episode_danmaku_prefetch_state(self, session: PlayerSession) -> None:
        session.prefetched_next_danmaku_indices.clear()
        self._invalidate_pending_next_episode_danmaku_prefetch(session)
        controller = session.danmaku_controller
        if controller is None:
            return
        invalidate = getattr(controller, "invalidate_running_danmaku_prefetches", None)
        if callable(invalidate):
            invalidate()

    def _invalidate_pending_next_episode_danmaku_prefetch(self, session: PlayerSession) -> None:
        session.pending_next_danmaku_prefetch_token += 1

    def _schedule_delayed_next_episode_danmaku_prefetch(
        self,
        session: PlayerSession,
        current_index: int,
    ) -> None:
        next_index = current_index + 1
        if not (0 <= next_index < len(session.playlist)):
            return
        if next_index in session.prefetched_next_danmaku_indices:
            return
        controller = session.danmaku_controller
        if controller is None:
            return
        prefetcher = getattr(controller, "prefetch_next_episode_danmaku", None)
        if not callable(prefetcher):
            return
        self._invalidate_pending_next_episode_danmaku_prefetch(session)
        token = session.pending_next_danmaku_prefetch_token

        def run_if_still_current() -> None:
            if token != session.pending_next_danmaku_prefetch_token:
                return
            self._schedule_next_episode_danmaku_prefetch(session, current_index)

        timer = self._prefetch_timer_factory(
            self._NEXT_EPISODE_DANMAKU_PREFETCH_DELAY_SECONDS,
            run_if_still_current,
        )
        timer.start()

    def on_item_started(self, session: PlayerSession, current_index: int) -> None:
        self._schedule_delayed_next_episode_danmaku_prefetch(session, current_index)

    def _schedule_next_episode_danmaku_prefetch(
        self,
        session: PlayerSession,
        current_index: int,
    ) -> None:
        next_index = current_index + 1
        if not (0 <= next_index < len(session.playlist)):
            return
        if next_index in session.prefetched_next_danmaku_indices:
            return
        controller = session.danmaku_controller
        if controller is None:
            return
        prefetcher = getattr(controller, "prefetch_next_episode_danmaku", None)
        if not callable(prefetcher):
            return
        session.prefetched_next_danmaku_indices.add(next_index)
        try:
            prefetcher(session.playlist[next_index], session.playlist)
        except Exception:
            session.prefetched_next_danmaku_indices.discard(next_index)
            logger.exception(
                "Prefetch next episode danmaku failed vod_id=%s next_index=%s",
                session.vod.vod_id,
                next_index,
            )
