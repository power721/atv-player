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


def test_parse_m3u_parses_http_user_agent_and_http_headers() -> None:
    playlist = """#EXTM3U
#EXTINF:-1 http-user-agent="AptvPlayer-UA" http-header="Referer=https://site.example/&Origin=https://origin.example" group-title="卫视",江苏卫视
https://live.example/jsws.m3u8
"""

    parsed = parse_m3u(playlist)

    assert parsed.groups[0].channels[0].headers == {
        "User-Agent": "AptvPlayer-UA",
        "Referer": "https://site.example/",
        "Origin": "https://origin.example",
    }


def test_parse_m3u_ignores_malformed_http_header_segments() -> None:
    playlist = """#EXTM3U
#EXTINF:-1 http-header="broken&Referer=https://site.example/" ,测试台
https://live.example/test.m3u8
"""

    parsed = parse_m3u(playlist)

    assert parsed.ungrouped_channels[0].headers == {
        "Referer": "https://site.example/",
    }
