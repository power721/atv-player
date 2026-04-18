from atv_player.live_playlist_parser import parse_live_playlist


def test_parse_live_playlist_dispatches_extm3u_to_m3u_parser() -> None:
    playlist = """#EXTM3U
#EXTINF:-1 group-title="央视频道",CCTV-1综合
https://live.example/cctv1.m3u8
"""

    parsed = parse_live_playlist(playlist)

    assert [group.name for group in parsed.groups] == ["央视频道"]
    assert [(item.name, item.url) for item in parsed.groups[0].channels] == [
        ("CCTV-1综合", "https://live.example/cctv1.m3u8")
    ]


def test_parse_live_playlist_parses_txt_group_rows_and_duplicate_channels() -> None:
    playlist = """🇨🇳IPV4线路,#genre#
CCTV-1,http://107.150.60.122/live/cctv1hd.m3u8
CCTV-1,http://63.141.230.178:82/gslb/zbdq5.m3u8?id=cctv1hd
"""

    parsed = parse_live_playlist(playlist)

    assert [group.name for group in parsed.groups] == ["🇨🇳IPV4线路"]
    assert [(item.name, item.url) for item in parsed.groups[0].channels] == [
        ("CCTV-1", "http://107.150.60.122/live/cctv1hd.m3u8"),
        ("CCTV-1", "http://63.141.230.178:82/gslb/zbdq5.m3u8?id=cctv1hd"),
    ]
