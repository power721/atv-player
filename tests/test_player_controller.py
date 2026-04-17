from atv_player.controllers.player_controller import PlayerController
from atv_player.models import HistoryRecord, PlayItem, VodItem


class FakeApiClient:
    def __init__(self) -> None:
        self.saved_payloads: list[dict] = []
        self.history: HistoryRecord | None = None
        self.history_calls: list[str] = []

    def get_history(self, key: str):
        self.history_calls.append(key)
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
        paused=False,
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


def test_player_controller_skips_local_history_when_session_disables_it() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    vod = VodItem(vod_id="emby-1", vod_name="Emby Movie")
    playlist = [PlayItem(title="Episode 1", url="", vod_id="1-3458")]

    session = controller.create_session(vod, playlist, clicked_index=0, use_local_history=False)

    assert api.history_calls == []
    assert session.start_index == 0
    assert session.start_position_seconds == 0
    assert session.speed == 1.0


def test_player_controller_can_restore_history_without_saving_local_history() -> None:
    api = FakeApiClient()
    api.history = HistoryRecord(
        id=1,
        key="emby-1",
        vod_name="Emby Movie",
        vod_pic="pic",
        vod_remarks="Episode 2",
        episode=1,
        episode_url="2.m3u8",
        position=45000,
        opening=5000,
        ending=10000,
        speed=1.25,
        create_time=1,
    )
    controller = PlayerController(api)
    vod = VodItem(vod_id="emby-1", vod_name="Emby Movie")
    playlist = [PlayItem(title="Episode 1", url="1.m3u8"), PlayItem(title="Episode 2", url="2.m3u8")]

    session = controller.create_session(
        vod,
        playlist,
        clicked_index=0,
        use_local_history=False,
        restore_history=True,
    )

    assert api.history_calls == ["emby-1"]
    assert session.start_index == 1
    assert session.start_position_seconds == 45
    assert session.speed == 1.25


def test_player_controller_reports_progress_via_session_hook_without_saving_history() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    vod = VodItem(vod_id="emby-1", vod_name="Emby Movie")
    playlist = [PlayItem(title="Episode 1", url="", vod_id="1-3458")]
    progress_calls: list[tuple[str, int, bool]] = []

    session = controller.create_session(
        vod,
        playlist,
        clicked_index=0,
        use_local_history=False,
        playback_progress_reporter=lambda item, position_ms, paused: progress_calls.append(
            (item.vod_id, position_ms, paused)
        ),
        playback_stopper=lambda item: progress_calls.append((item.vod_id, -1, False)),
    )

    controller.report_progress(
        session,
        current_index=0,
        position_seconds=90,
        speed=1.25,
        opening_seconds=15,
        ending_seconds=30,
        paused=False,
    )
    controller.stop_playback(session, current_index=0)

    assert progress_calls == [("1-3458", 90000, False), ("1-3458", -1, False)]
    assert api.saved_payloads == []


def test_player_controller_forwards_paused_state_to_progress_reporter() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    vod = VodItem(vod_id="emby-1", vod_name="Emby Movie")
    playlist = [PlayItem(title="Episode 1", url="", vod_id="1-3458")]
    progress_calls: list[tuple[str, int, bool]] = []

    session = controller.create_session(
        vod,
        playlist,
        clicked_index=0,
        playback_progress_reporter=lambda item, position_ms, paused: progress_calls.append(
            (item.vod_id, position_ms, paused)
        ),
    )

    controller.report_progress(
        session,
        current_index=0,
        position_seconds=45,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
        paused=True,
    )

    assert progress_calls == [("1-3458", 45000, True)]
