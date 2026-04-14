from atv_player.controllers.player_controller import PlayerController
from atv_player.models import HistoryRecord, PlayItem, VodItem


class FakeApiClient:
    def __init__(self) -> None:
        self.saved_payloads: list[dict] = []
        self.history: HistoryRecord | None = None

    def get_history(self, key: str):
        return self.history

    def save_history(self, payload: dict) -> None:
        self.saved_payloads.append(payload)


def test_player_controller_restores_resume_state() -> None:
    api = FakeApiClient()
    api.history = HistoryRecord(
        id=1,
        key="movie-1",
        vod_name="Movie",
        vod_pic="pic",
        vod_remarks="Episode 2",
        episode=1,
        episode_url="2.m3u8",
        position=45000,
        opening=0,
        ending=0,
        speed=1.5,
        create_time=1,
    )
    controller = PlayerController(api)
    vod = VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="pic")
    playlist = [PlayItem(title="Episode 1", url="1.m3u8"), PlayItem(title="Episode 2", url="2.m3u8")]

    session = controller.create_session(vod, playlist, clicked_index=0)

    assert session.start_index == 1
    assert session.start_position_seconds == 45
    assert session.speed == 1.5


def test_player_controller_builds_history_payload() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    vod = VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="pic")
    playlist = [PlayItem(title="Episode 1", url="1.m3u8"), PlayItem(title="Episode 2", url="2.m3u8")]
    session = controller.create_session(vod, playlist, clicked_index=1)

    controller.report_progress(session, current_index=1, position_seconds=90, speed=1.25)

    payload = api.saved_payloads[0]
    assert payload["key"] == "movie-1"
    assert payload["vodName"] == "Movie"
    assert payload["episode"] == 1
    assert payload["episodeUrl"] == "2.m3u8"
    assert payload["position"] == 90000
    assert payload["speed"] == 1.25
