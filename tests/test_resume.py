from atv_player.models import HistoryRecord, PlayItem
from atv_player.player.resume import resolve_resume_index


def test_resolve_resume_index_prefers_episode() -> None:
    playlist = [PlayItem(title="1", url="http://m/1.m3u8"), PlayItem(title="2", url="http://m/2.m3u8")]
    history = HistoryRecord(
        id=1,
        key="abc",
        vod_name="Movie",
        vod_pic="",
        vod_remarks="Ep2",
        episode=1,
        episode_url="2.m3u8",
        position=12000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1,
    )

    assert resolve_resume_index(history, playlist, clicked_index=0) == 1


def test_resolve_resume_index_falls_back_to_episode_url_filename() -> None:
    playlist = [PlayItem(title="1", url="http://m/1.m3u8?token=a"), PlayItem(title="2", url="http://m/2.m3u8?token=b")]
    history = HistoryRecord(
        id=1,
        key="abc",
        vod_name="Movie",
        vod_pic="",
        vod_remarks="Ep2",
        episode=-1,
        episode_url="2.m3u8",
        position=12000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1,
    )

    assert resolve_resume_index(history, playlist, clicked_index=0) == 1
