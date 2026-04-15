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
        opening=12000,
        ending=24000,
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
    assert session.opening_seconds == 12
    assert session.ending_seconds == 24


def test_player_controller_builds_history_payload() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    vod = VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="pic")
    playlist = [PlayItem(title="Episode 1", url="1.m3u8"), PlayItem(title="Episode 2", url="2.m3u8")]
    session = controller.create_session(vod, playlist, clicked_index=1)

    controller.report_progress(
        session,
        current_index=1,
        position_seconds=90,
        speed=1.25,
        opening_seconds=15,
        ending_seconds=30,
    )

    payload = api.saved_payloads[0]
    assert payload["key"] == "movie-1"
    assert payload["vodName"] == "Movie"
    assert payload["episode"] == 1
    assert payload["episodeUrl"] == "2.m3u8"
    assert payload["position"] == 90000
    assert payload["opening"] == 15000
    assert payload["ending"] == 30000
    assert payload["speed"] == 1.25


def test_player_controller_create_session_preserves_detail_resolver_and_seed_cache() -> None:
    controller = PlayerController(FakeApiClient())
    vod = VodItem(vod_id="movie-1", vod_name="Movie")
    playlist = [PlayItem(title="Episode 1", url="", vod_id="1$91483$1")]
    resolved_vod = VodItem(
        vod_id="1$91483$1",
        vod_name="Resolved Episode",
        vod_play_url="http://m/1.m3u8",
        items=[PlayItem(title="Episode 1", url="http://m/1.m3u8", vod_id="1$91483$1")],
    )

    def detail_resolver(item: PlayItem) -> VodItem:
        raise AssertionError("resolver should not be called when the cache is pre-seeded")

    session = controller.create_session(
        vod,
        playlist,
        clicked_index=0,
        detail_resolver=detail_resolver,
        resolved_vod_by_id={"1$91483$1": resolved_vod},
    )

    assert session.detail_resolver is detail_resolver
    assert session.resolved_vod_by_id["1$91483$1"].vod_name == "Resolved Episode"


def test_player_controller_resolve_play_item_detail_uses_session_cache() -> None:
    controller = PlayerController(FakeApiClient())
    vod = VodItem(vod_id="movie-1", vod_name="Movie")
    playlist = [PlayItem(title="Episode 1", url="", vod_id="1$91483$1")]
    calls: list[str] = []

    def detail_resolver(item: PlayItem) -> VodItem:
        calls.append(item.vod_id)
        return VodItem(
            vod_id=item.vod_id,
            vod_name="Resolved Episode",
            vod_play_url="http://m/1.m3u8",
            items=[PlayItem(title="Episode 1", url="http://m/1.m3u8", vod_id=item.vod_id)],
        )

    session = controller.create_session(vod, playlist, clicked_index=0, detail_resolver=detail_resolver)

    first = controller.resolve_play_item_detail(session, playlist[0])
    second = controller.resolve_play_item_detail(session, playlist[0])

    assert calls == ["1$91483$1"]
    assert first.vod_name == "Resolved Episode"
    assert second.vod_name == "Resolved Episode"
    assert playlist[0].url == "http://m/1.m3u8"


def test_player_controller_resolve_play_item_detail_handles_missing_detail() -> None:
    controller = PlayerController(FakeApiClient())
    vod = VodItem(vod_id="movie-1", vod_name="Movie")
    playlist = [PlayItem(title="Episode 1", url="http://m/existing.m3u8", vod_id="1$91483$1")]
    calls: list[str] = []

    def detail_resolver(item: PlayItem) -> None:
        calls.append(item.vod_id)
        return None

    session = controller.create_session(vod, playlist, clicked_index=0, detail_resolver=detail_resolver)

    resolved = controller.resolve_play_item_detail(session, playlist[0])

    assert calls == ["1$91483$1"]
    assert resolved is None
    assert playlist[0].url == "http://m/existing.m3u8"
    assert session.resolved_vod_by_id == {}
