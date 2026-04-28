import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from time import time

from atv_player.models import HistoryRecord, PlayItem, PlaybackLoadResult, VodItem
from atv_player.player.resume import resolve_resume_index


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PlayerSession:
    vod: VodItem
    playlist: list[PlayItem]
    start_index: int
    start_position_seconds: int
    speed: float
    playlists: list[list[PlayItem]] = field(default_factory=list)
    playlist_index: int = 0
    opening_seconds: int = 0
    ending_seconds: int = 0
    detail_resolver: Callable[[PlayItem], VodItem | None] | None = None
    resolved_vod_by_id: dict[str, VodItem] = field(default_factory=dict)
    use_local_history: bool = True
    playback_loader: Callable[[PlayItem], PlaybackLoadResult | None] | None = None
    danmaku_controller: object | None = None
    playback_progress_reporter: Callable[[PlayItem, int, bool], None] | None = None
    playback_stopper: Callable[[PlayItem], None] | None = None
    playback_history_saver: Callable[[dict[str, object]], None] | None = None


class PlayerController:
    def __init__(self, api_client) -> None:
        self._api_client = api_client

    def _normalize_playlists(
        self,
        playlist: list[PlayItem],
        playlists: list[list[PlayItem]] | None,
        playlist_index: int,
    ) -> tuple[list[list[PlayItem]], int, list[PlayItem]]:
        normalized = [group for group in (playlists or []) if group]
        if not normalized:
            normalized = [playlist]
        playlist_index = max(0, min(playlist_index, len(normalized) - 1))
        return normalized, playlist_index, normalized[playlist_index]

    def _restore_playlist_group(
        self,
        normalized_playlists: list[list[PlayItem]],
        playlist_index: int,
        history: HistoryRecord | None,
    ) -> tuple[int, list[PlayItem]]:
        if history is not None and 0 <= history.playlist_index < len(normalized_playlists):
            playlist_index = history.playlist_index
        return playlist_index, normalized_playlists[playlist_index]

    def create_session(
        self,
        vod: VodItem,
        playlist: list[PlayItem],
        clicked_index: int,
        playlists: list[list[PlayItem]] | None = None,
        playlist_index: int = 0,
        detail_resolver: Callable[[PlayItem], VodItem | None] | None = None,
        resolved_vod_by_id: dict[str, VodItem] | None = None,
        use_local_history: bool = True,
        restore_history: bool = False,
        playback_loader: Callable[[PlayItem], PlaybackLoadResult | None] | None = None,
        danmaku_controller: object | None = None,
        playback_progress_reporter: Callable[[PlayItem, int, bool], None] | None = None,
        playback_stopper: Callable[[PlayItem], None] | None = None,
        playback_history_loader: Callable[[], HistoryRecord | None] | None = None,
        playback_history_saver: Callable[[dict[str, object]], None] | None = None,
    ) -> PlayerSession:
        normalized_playlists, playlist_index, active_playlist = self._normalize_playlists(
            playlist,
            playlists,
            playlist_index,
        )
        history = playback_history_loader() if playback_history_loader is not None else None
        if history is None and (use_local_history or restore_history):
            history = self._api_client.get_history(vod.vod_id)
        playlist_index, active_playlist = self._restore_playlist_group(
            normalized_playlists,
            playlist_index,
            history,
        )
        start_index = resolve_resume_index(history, active_playlist, clicked_index)
        matched_history = history is not None and (
            start_index == history.episode or playback_history_loader is not None
        )
        if matched_history and history is not None:
            position_seconds = int(history.position / 1000)
            speed = history.speed
        else:
            position_seconds = 0
            speed = 1.0
        logger.info(
            "Create player session vod_id=%s playlist_size=%s start_index=%s restored=%s",
            vod.vod_id,
            len(active_playlist),
            start_index,
            matched_history,
        )
        return PlayerSession(
            vod=vod,
            playlist=active_playlist,
            start_index=start_index,
            start_position_seconds=position_seconds,
            speed=speed,
            playlists=normalized_playlists,
            playlist_index=playlist_index,
            opening_seconds=int((history.opening if history else 0) / 1000),
            ending_seconds=int((history.ending if history else 0) / 1000),
            detail_resolver=detail_resolver,
            resolved_vod_by_id=dict(resolved_vod_by_id or {}),
            use_local_history=use_local_history,
            playback_loader=playback_loader,
            danmaku_controller=danmaku_controller,
            playback_progress_reporter=playback_progress_reporter,
            playback_stopper=playback_stopper,
            playback_history_saver=playback_history_saver,
        )

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
    ) -> None:
        if not (0 <= current_index < len(session.playlist)):
            return
        current_item = session.playlist[current_index]
        position_ms = position_seconds * 1000
        if session.playback_progress_reporter is not None and (not paused or force_remote_report):
            session.playback_progress_reporter(current_item, position_ms, paused)
        logger.info(
            "Report playback progress vod_id=%s index=%s position_ms=%s paused=%s",
            session.vod.vod_id,
            current_index,
            position_ms,
            paused,
        )
        payload = {
            "cid": 0,
            "key": session.vod.vod_id,
            "vodName": session.vod.vod_name,
            "vodPic": session.vod.vod_pic,
            "vodRemarks": current_item.title,
            "episode": current_index,
            "episodeUrl": current_item.url,
            "position": position_ms,
            "opening": opening_seconds * 1000,
            "ending": ending_seconds * 1000,
            "speed": speed,
            "playlistIndex": session.playlist_index,
            "createTime": int(time() * 1000),
        }
        if session.playback_history_saver is not None:
            session.playback_history_saver(payload)
        if not session.use_local_history:
            return
        self._api_client.save_history(payload)

    def stop_playback(self, session: PlayerSession, current_index: int) -> None:
        if session.playback_stopper is None:
            return
        if not (0 <= current_index < len(session.playlist)):
            return
        logger.info("Stop playback vod_id=%s index=%s", session.vod.vod_id, current_index)
        session.playback_stopper(session.playlist[current_index])
