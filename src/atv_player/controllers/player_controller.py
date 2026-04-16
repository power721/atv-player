from collections.abc import Callable
from dataclasses import dataclass, field
from time import time

from atv_player.models import PlayItem, VodItem
from atv_player.player.resume import resolve_resume_index


@dataclass(slots=True)
class PlayerSession:
    vod: VodItem
    playlist: list[PlayItem]
    start_index: int
    start_position_seconds: int
    speed: float
    opening_seconds: int = 0
    ending_seconds: int = 0
    detail_resolver: Callable[[PlayItem], VodItem | None] | None = None
    resolved_vod_by_id: dict[str, VodItem] = field(default_factory=dict)
    use_local_history: bool = True
    playback_loader: Callable[[PlayItem], None] | None = None
    playback_progress_reporter: Callable[[PlayItem, int], None] | None = None
    playback_stopper: Callable[[PlayItem], None] | None = None


class PlayerController:
    def __init__(self, api_client) -> None:
        self._api_client = api_client

    def create_session(
        self,
        vod: VodItem,
        playlist: list[PlayItem],
        clicked_index: int,
        detail_resolver: Callable[[PlayItem], VodItem | None] | None = None,
        resolved_vod_by_id: dict[str, VodItem] | None = None,
        use_local_history: bool = True,
        playback_loader: Callable[[PlayItem], None] | None = None,
        playback_progress_reporter: Callable[[PlayItem, int], None] | None = None,
        playback_stopper: Callable[[PlayItem], None] | None = None,
    ) -> PlayerSession:
        history = self._api_client.get_history(vod.vod_id) if use_local_history else None
        start_index = resolve_resume_index(history, playlist, clicked_index)
        matched_history = history and start_index == history.episode
        position_seconds = int((history.position if matched_history else 0) / 1000)
        speed = history.speed if matched_history else 1.0
        return PlayerSession(
            vod=vod,
            playlist=playlist,
            start_index=start_index,
            start_position_seconds=position_seconds,
            speed=speed,
            opening_seconds=int((history.opening if history else 0) / 1000),
            ending_seconds=int((history.ending if history else 0) / 1000),
            detail_resolver=detail_resolver,
            resolved_vod_by_id=dict(resolved_vod_by_id or {}),
            use_local_history=use_local_history,
            playback_loader=playback_loader,
            playback_progress_reporter=playback_progress_reporter,
            playback_stopper=playback_stopper,
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
            if resolved_vod is not None:
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
    ) -> None:
        if not (0 <= current_index < len(session.playlist)):
            return
        current_item = session.playlist[current_index]
        position_ms = position_seconds * 1000
        if session.playback_progress_reporter is not None:
            session.playback_progress_reporter(current_item, position_ms)
        if not session.use_local_history:
            return
        self._api_client.save_history(
            {
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
                "createTime": int(time() * 1000),
            }
        )

    def stop_playback(self, session: PlayerSession, current_index: int) -> None:
        if session.playback_stopper is None:
            return
        if not (0 <= current_index < len(session.playlist)):
            return
        session.playback_stopper(session.playlist[current_index])
