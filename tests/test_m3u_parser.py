from atv_player.m3u_parser import parse_m3u


def test_parse_m3u_groups_channels_by_group_title() -> None:
    playlist = """#EXTM3U
#EXTINF:-1 group-title="央视频道",CCTV-1综合
https://live.example/cctv1.m3u8
#EXTINF:-1 group-title="央视频道",CCTV-2财经
https://live.example/cctv2.m3u8
"""

    parsed = parse_m3u(playlist)

    assert [group.name for group in parsed.groups] == ["央视频道"]
    assert [(item.name, item.url) for item in parsed.groups[0].channels] == [
        ("CCTV-1综合", "https://live.example/cctv1.m3u8"),
        ("CCTV-2财经", "https://live.example/cctv2.m3u8"),
    ]


def test_parse_m3u_keeps_ungrouped_channels_separately() -> None:
    playlist = """#EXTM3U
#EXTINF:-1 tvg-name="CGTN",CGTN英语
https://live.example/cgtn.m3u8
"""

    parsed = parse_m3u(playlist)

    assert parsed.groups == []
    assert [(item.name, item.url) for item in parsed.ungrouped_channels] == [
        ("CGTN英语", "https://live.example/cgtn.m3u8")
    ]


def test_parse_m3u_ignores_comments_and_reads_optional_logo() -> None:
    playlist = """#EXTM3U
#EXTINF:-1 tvg-logo="https://img.example/logo.png" group-title="卫视频道",北京卫视
https://live.example/btv.m3u8
# some comment
"""

    parsed = parse_m3u(playlist)

    assert parsed.groups[0].channels[0].logo_url == "https://img.example/logo.png"
