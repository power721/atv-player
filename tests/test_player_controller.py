import logging

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


def test_player_controller_prefers_plugin_local_history_loader() -> None:
    api = FakeApiClient()
    api.history = HistoryRecord(
        id=1,
        key="movie-1",
        vod_name="API Movie",
        vod_pic="api-pic",
        vod_remarks="Episode 1",
        episode=0,
        episode_url="1.m3u8",
        position=1000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1,
    )
    controller = PlayerController(api)
    vod = VodItem(vod_id="movie-1", vod_name="Plugin Movie", vod_pic="plugin-pic")
    playlist = [PlayItem(title="Episode 1", url="1.m3u8"), PlayItem(title="Episode 2", url="2.m3u8")]

    session = controller.create_session(
        vod,
        playlist,
        clicked_index=0,
        use_local_history=False,
        playback_history_loader=lambda: HistoryRecord(
            id=0,
            key="plugin:movie-1",
            vod_name="Plugin Movie",
            vod_pic="plugin-pic",
            vod_remarks="Episode 2",
            episode=1,
            episode_url="2.m3u8",
            position=45000,
            opening=5000,
            ending=10000,
            speed=1.25,
            create_time=2,
        ),
    )

    assert api.history_calls == []
    assert session.start_index == 1
    assert session.start_position_seconds == 45
    assert session.speed == 1.25
    assert session.opening_seconds == 5
    assert session.ending_seconds == 10


def test_player_controller_preserves_history_progress_for_plugin_placeholder_playlist() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    vod = VodItem(vod_id="/detail/drive", vod_name="Plugin Movie", vod_pic="plugin-pic")
    playlist = [PlayItem(title="查看", url="", vod_id="https://pan.baidu.com/s/demo")]

    session = controller.create_session(
        vod,
        playlist,
        clicked_index=0,
        use_local_history=False,
        playback_history_loader=lambda: HistoryRecord(
            id=0,
            key="/detail/drive",
            vod_name="Plugin Movie",
            vod_pic="plugin-pic",
            vod_remarks="第2集",
            episode=1,
            episode_url="http://m/2.mp4",
            position=45000,
            opening=5000,
            ending=10000,
            speed=1.25,
            create_time=2,
        ),
    )

    assert session.start_index == 0
    assert session.start_position_seconds == 45
    assert session.speed == 1.25
    assert session.opening_seconds == 5
    assert session.ending_seconds == 10


def test_player_controller_prefers_emby_local_history_loader() -> None:
    api = FakeApiClient()
    api.history = HistoryRecord(
        id=1,
        key="emby-1",
        vod_name="API Emby Movie",
        vod_pic="api-pic",
        vod_remarks="Episode 1",
        episode=0,
        episode_url="1.m3u8",
        position=1000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1,
    )
    controller = PlayerController(api)
    vod = VodItem(vod_id="emby-1", vod_name="Emby Movie", vod_pic="emby-pic")
    playlist = [PlayItem(title="Episode 1", url="1.m3u8"), PlayItem(title="Episode 2", url="2.m3u8")]

    session = controller.create_session(
        vod,
        playlist,
        clicked_index=0,
        use_local_history=False,
        playback_history_loader=lambda: HistoryRecord(
            id=0,
            key="emby-1",
            vod_name="Emby Movie",
            vod_pic="emby-pic",
            vod_remarks="Episode 2",
            episode=1,
            episode_url="2.m3u8",
            position=45000,
            opening=5000,
            ending=10000,
            speed=1.25,
            create_time=2,
        ),
    )

    assert api.history_calls == []
    assert session.start_index == 1
    assert session.start_position_seconds == 45
    assert session.speed == 1.25


def test_player_controller_reports_progress_to_plugin_local_saver_without_backend_history() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    vod = VodItem(vod_id="plugin-1", vod_name="Plugin Movie", vod_pic="poster")
    playlist = [PlayItem(title="第1集", url="https://media.example/1.m3u8")]
    saved_payloads: list[dict[str, object]] = []

    session = controller.create_session(
        vod,
        playlist,
        clicked_index=0,
        use_local_history=False,
        playback_history_saver=lambda payload: saved_payloads.append(payload),
    )

    controller.report_progress(
        session,
        current_index=0,
        position_seconds=45,
        speed=1.25,
        opening_seconds=5,
        ending_seconds=10,
        paused=False,
    )

    assert len(saved_payloads) == 1
    assert saved_payloads[0]["key"] == "plugin-1"
    assert api.saved_payloads == []


def test_player_controller_reports_progress_to_jellyfin_local_saver_without_backend_history() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    vod = VodItem(vod_id="jf-1", vod_name="Jellyfin Movie", vod_pic="poster")
    playlist = [PlayItem(title="Episode 1", url="https://media.example/1.m3u8")]
    saved_payloads: list[dict[str, object]] = []

    session = controller.create_session(
        vod,
        playlist,
        clicked_index=0,
        use_local_history=False,
        playback_history_saver=lambda payload: saved_payloads.append(payload),
    )

    controller.report_progress(
        session,
        current_index=0,
        position_seconds=45,
        speed=1.25,
        opening_seconds=5,
        ending_seconds=10,
        paused=False,
    )

    assert len(saved_payloads) == 1
    assert saved_payloads[0]["key"] == "jf-1"
    assert api.saved_payloads == []


def test_player_controller_logs_session_creation(caplog) -> None:
    controller = PlayerController(FakeApiClient())
    vod = VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="pic")
    playlist = [PlayItem(title="Episode 1", url="1.m3u8")]

    with caplog.at_level(logging.INFO):
        controller.create_session(vod, playlist, clicked_index=0)

    assert "Create player session" in caplog.text
    assert "movie-1" in caplog.text


def test_player_controller_logs_progress_reporting(caplog) -> None:
    controller = PlayerController(FakeApiClient())
    vod = VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="pic")
    playlist = [PlayItem(title="Episode 1", url="1.m3u8")]
    session = controller.create_session(vod, playlist, clicked_index=0)

    with caplog.at_level(logging.INFO):
        controller.report_progress(
            session,
            current_index=0,
            position_seconds=12,
            speed=1.0,
            opening_seconds=0,
            ending_seconds=0,
            paused=False,
        )

    assert "Report playback progress" in caplog.text
    assert "movie-1" in caplog.text


def test_player_controller_restores_selected_playlist_group_from_history_loader() -> None:
    controller = PlayerController(FakeApiClient())
    vod = VodItem(vod_id="plugin-vod-1", vod_name="Plugin Movie", vod_pic="poster-plugin")
    first_group = [
        PlayItem(title="第1集", url="https://backup.example/1.m3u8", play_source="备用线"),
        PlayItem(title="第2集", url="https://backup.example/2.m3u8", play_source="备用线"),
    ]
    second_group = [
        PlayItem(title="第1集", url="https://fast.example/1.m3u8", play_source="极速线"),
        PlayItem(title="第2集", url="https://fast.example/2.m3u8", play_source="极速线"),
    ]

    session = controller.create_session(
        vod,
        playlist=first_group,
        clicked_index=0,
        playlists=[first_group, second_group],
        playlist_index=0,
        use_local_history=False,
        playback_history_loader=lambda: HistoryRecord(
            id=0,
            key="plugin:plugin-vod-1",
            vod_name="Plugin Movie",
            vod_pic="poster-plugin",
            vod_remarks="第2集",
            episode=1,
            episode_url="https://fast.example/2.m3u8",
            position=45000,
            opening=5000,
            ending=10000,
            speed=1.25,
            create_time=2,
            playlist_index=1,
        ),
    )

    assert session.playlist_index == 1
    assert session.playlist is second_group
    assert session.start_index == 1
    assert session.start_position_seconds == 45
    assert session.speed == 1.25


def test_player_controller_reports_progress_to_plugin_local_saver_without_api_history() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    vod = VodItem(vod_id="plugin-vod-1", vod_name="Plugin Movie", vod_pic="poster-plugin")
    playlist = [PlayItem(title="Episode 1", url="https://media.example/1.m3u8", vod_id="ep-1")]
    saved_payloads: list[dict[str, object]] = []

    session = controller.create_session(
        vod,
        playlist,
        clicked_index=0,
        use_local_history=False,
        playback_history_saver=lambda payload: saved_payloads.append(payload),
    )

    controller.report_progress(
        session,
        current_index=0,
        position_seconds=90,
        speed=1.5,
        opening_seconds=15,
        ending_seconds=30,
        paused=False,
    )

    assert api.saved_payloads == []
    assert len(saved_payloads) == 1
    assert saved_payloads[0]["key"] == "plugin-vod-1"
    assert saved_payloads[0]["vodName"] == "Plugin Movie"
    assert saved_payloads[0]["vodPic"] == "poster-plugin"
    assert saved_payloads[0]["vodRemarks"] == "Episode 1"
    assert saved_payloads[0]["episode"] == 0
    assert saved_payloads[0]["episodeUrl"] == "https://media.example/1.m3u8"
    assert saved_payloads[0]["position"] == 90000
    assert saved_payloads[0]["opening"] == 15000
    assert saved_payloads[0]["ending"] == 30000
    assert saved_payloads[0]["speed"] == 1.5
    assert saved_payloads[0]["playlistIndex"] == 0
    assert isinstance(saved_payloads[0]["createTime"], int)


def test_player_controller_reports_selected_playlist_index_to_plugin_local_saver() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    vod = VodItem(vod_id="plugin-vod-1", vod_name="Plugin Movie", vod_pic="poster-plugin")
    first_group = [PlayItem(title="第1集", url="https://backup.example/1.m3u8", play_source="备用线")]
    second_group = [PlayItem(title="第1集", url="https://fast.example/1.m3u8", play_source="极速线")]
    saved_payloads: list[dict[str, object]] = []

    session = controller.create_session(
        vod,
        playlist=second_group,
        clicked_index=0,
        playlists=[first_group, second_group],
        playlist_index=1,
        use_local_history=False,
        playback_history_saver=lambda payload: saved_payloads.append(payload),
    )

    controller.report_progress(
        session,
        current_index=0,
        position_seconds=30,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
        paused=False,
    )

    assert api.saved_payloads == []
    assert saved_payloads[0]["playlistIndex"] == 1


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


def test_player_controller_normalizes_single_playlist_into_one_group() -> None:
    controller = PlayerController(FakeApiClient())
    vod = VodItem(vod_id="movie-1", vod_name="Movie")
    playlist = [PlayItem(title="Episode 1", url="1.m3u8"), PlayItem(title="Episode 2", url="2.m3u8")]

    session = controller.create_session(vod, playlist, clicked_index=1)

    assert len(session.playlists) == 1
    assert session.playlist_index == 0
    assert [item.title for item in session.playlists[0]] == ["Episode 1", "Episode 2"]
    assert session.playlist is session.playlists[0]
    assert session.start_index == 1


def test_player_controller_uses_selected_group_as_active_playlist() -> None:
    controller = PlayerController(FakeApiClient())
    vod = VodItem(vod_id="plugin-1", vod_name="Plugin Movie")
    first_group = [PlayItem(title="第1集", url="http://m/1.m3u8", play_source="备用线")]
    second_group = [
        PlayItem(title="第1集", url="http://b/1.m3u8", play_source="极速线"),
        PlayItem(title="第2集", url="http://b/2.m3u8", play_source="极速线"),
    ]

    session = controller.create_session(
        vod,
        playlist=second_group,
        clicked_index=1,
        playlists=[first_group, second_group],
        playlist_index=1,
    )

    assert len(session.playlists) == 2
    assert session.playlist_index == 1
    assert session.playlist is second_group
    assert [item.title for item in session.playlist] == ["第1集", "第2集"]
    assert session.start_index == 1
