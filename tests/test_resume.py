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


def test_resolve_resume_index_prefers_matching_episode_url_over_stale_episode_number() -> None:
    playlist = [
        PlayItem(title="1", url="http://m/1.m3u8?token=a"),
        PlayItem(title="2", url="http://m/2.m3u8?token=b"),
    ]
    history = HistoryRecord(
        id=1,
        key="abc",
        vod_name="Movie",
        vod_pic="",
        vod_remarks="Ep1",
        episode=1,
        episode_url="1.m3u8",
        position=12000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1,
    )

    assert resolve_resume_index(history, playlist, clicked_index=0) == 0


def test_resolve_resume_index_ignores_empty_basename_and_falls_back_to_episode() -> None:
    playlist = [PlayItem(title=f"{index + 1}", url="") for index in range(77)]
    history = HistoryRecord(
        id=1,
        key="abc",
        vod_name="Movie",
        vod_pic="",
        vod_remarks="Ep18",
        episode=17,
        episode_url="https://media.example/segment/?token=a",
        position=12000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1,
    )

    assert resolve_resume_index(history, playlist, clicked_index=0) == 17


def _history_with_drive_path(drive_path: str, episode: int = 0) -> HistoryRecord:
    return HistoryRecord(
        id=1,
        key="gy_tv_58kD",
        vod_name="菜鸟老警",
        vod_pic="",
        vod_remarks="S02E03.mp4",
        episode=episode,
        episode_url="",
        position=12000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1,
        drive_share_key="baidu@1fFDWZTTtXy8aTPjKJ2F0uA@f1z9",
        drive_path=drive_path,
    )


def test_resolve_resume_index_prefers_drive_path_over_episode_number() -> None:
    # 列表重排后集数(episode=0)指向了别的文件,规范路径仍能定位到同一集
    playlist = [
        PlayItem(title="S02E01", url="http://m/1", path="/我的百度分享/temp/baidu@1fFDWZTTtXy8aTPjKJ2F0uA@f1z9/C 菜鸟老警/S02/S02E01.mp4"),
        PlayItem(title="S02E03", url="http://m/3", path="/我的百度分享/temp/baidu@1fFDWZTTtXy8aTPjKJ2F0uA@f1z9/C 菜鸟老警/S02/S02E03.mp4"),
    ]
    history = _history_with_drive_path("/C 菜鸟老警/S02/S02E03.mp4", episode=0)

    assert resolve_resume_index(history, playlist, clicked_index=0) == 1


def test_resolve_resume_index_drive_path_matches_across_reordered_directories() -> None:
    # 子目录改名/重排:相对路径不变即可命中;不匹配则退回集数
    playlist = [
        PlayItem(title="01", url="http://m/1", path="/我的百度分享/temp/baidu@1fFDWZTTtXy8aTPjKJ2F0uA@f1z9/改过的目录/S01/S01E01.mp4"),
        PlayItem(title="S02E03", url="http://m/3", path="/我的百度分享/temp/baidu@1fFDWZTTtXy8aTPjKJ2F0uA@f1z9/C 菜鸟老警/S02/S02E03.mp4"),
    ]
    history = _history_with_drive_path("/C 菜鸟老警/S02/S02E03.mp4", episode=0)

    assert resolve_resume_index(history, playlist, clicked_index=0) == 1


def test_resolve_resume_index_falls_back_when_drive_path_not_found() -> None:
    playlist = [
        PlayItem(title="1", url="http://m/1.m3u8"),
        PlayItem(title="2", url="http://m/2.m3u8"),
    ]
    history = _history_with_drive_path("/C 菜鸟老警/S02/S02E03.mp4", episode=1)

    # 播放列表没有网盘路径(如普通站点源),退回集数
    assert resolve_resume_index(history, playlist, clicked_index=0) == 1


def test_resolve_resume_index_matches_msubep_logical_token_over_stale_index() -> None:
    # 跨端拉回的 msub 历史:episode 下标失真为 0,真实集数在 msubep 逻辑 id 里
    history = HistoryRecord(
        id=1, key="msub:36", vod_name="盗妖行", vod_pic="", vod_remarks="",
        episode=0, episode_url="msubep-36-51", position=138_000,
        opening=0, ending=0, speed=1.0, create_time=1,
    )
    playlist = [
        PlayItem(title=f"第{ep}集", url="", play_id=f"msubep-36-{ep}")
        for ep in range(1, 57)
    ]
    # 集数空洞:43 不可播,playlist 缺 43,下标与集号错位
    playlist = [item for item in playlist if item.play_id != "msubep-36-43"]

    index = resolve_resume_index(history, playlist, clicked_index=45)

    assert playlist[index].play_id == "msubep-36-51"
    assert index != 0


def test_resolve_resume_index_matches_own_saved_msubep_with_suffix() -> None:
    # 本端上报的 episode_url 带 "@subgroup@index" 后缀,token 仍可命中
    history = HistoryRecord(
        id=1, key="msub:36", vod_name="盗妖行", vod_pic="", vod_remarks="",
        episode=12, episode_url="msubep-36-13@0@12", position=50_000,
        opening=0, ending=0, speed=1.0, create_time=1,
    )
    playlist = [
        PlayItem(title=f"第{ep}集", url="", play_id=f"msubep-36-{ep}")
        for ep in range(1, 21)
    ]

    index = resolve_resume_index(history, playlist, clicked_index=0)

    assert playlist[index].play_id == "msubep-36-13"


def test_resolve_resume_index_falls_through_when_token_not_in_playlist() -> None:
    history = HistoryRecord(
        id=1, key="msub:36", vod_name="盗妖行", vod_pic="", vod_remarks="",
        episode=2, episode_url="msubep-36-99", position=10_000,
        opening=0, ending=0, speed=1.0, create_time=1,
    )
    playlist = [
        PlayItem(title=f"第{ep}集", url="", play_id=f"msubep-36-{ep}")
        for ep in range(1, 11)
    ]

    index = resolve_resume_index(history, playlist, clicked_index=7)

    # token 找不到 → 依序落到 episode 下标兜底(而非 clicked)
    assert index == 2
