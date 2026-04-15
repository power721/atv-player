from dataclasses import dataclass
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


class PlayerController:
    def __init__(self, api_client) -> None:
        self._api_client = api_client

    def create_session(
        self,
        vod: VodItem,
        playlist: list[PlayItem],
        clicked_index: int,
    ) -> PlayerSession:
        history = self._api_client.get_history(vod.vod_id)
        start_index = resolve_resume_index(history, playlist, clicked_index)
        position_seconds = int((history.position if history else 0) / 1000)
        speed = history.speed if history else 1.0
        return PlayerSession(
            vod=vod,
            playlist=playlist,
            start_index=start_index,
            start_position_seconds=position_seconds,
            speed=speed,
            opening_seconds=int((history.opening if history else 0) / 1000),
            ending_seconds=int((history.ending if history else 0) / 1000),
        )

    def report_progress(
        self,
        session: PlayerSession,
        current_index: int,
        position_seconds: int,
        speed: float,
        opening_seconds: int,
        ending_seconds: int,
    ) -> None:
        current_item = session.playlist[current_index]
        self._api_client.save_history(
            {
                "cid": 0,
                "key": session.vod.vod_id,
                "vodName": session.vod.vod_name,
                "vodPic": session.vod.vod_pic,
                "vodRemarks": current_item.title,
                "episode": current_index,
                "episodeUrl": current_item.url,
                "position": position_seconds * 1000,
                "opening": opening_seconds * 1000,
                "ending": ending_seconds * 1000,
                "speed": speed,
                "createTime": int(time() * 1000),
            }
        )
