from atv_player.danmaku.subtitle import render_danmaku_srt


def test_render_danmaku_srt_builds_top_line_timeline_from_xml() -> None:
    xml_text = (
        '<?xml version="1.0" encoding="UTF-8"?><i>'
        '<d p="0.0,1,25,16777215">第一条</d>'
        '<d p="0.5,1,25,16777215">第二条</d>'
        '<d p="1.0,1,25,16777215">第三条</d>'
        '<d p="4.1,1,25,16777215">第四条</d>'
        "</i>"
    )

    subtitle = render_danmaku_srt(xml_text, line_count=2, duration_seconds=4.0)

    assert subtitle == "\n".join(
        [
            "1",
            "00:00:00,000 --> 00:00:00,500",
            "第一条",
            "",
            "2",
            "00:00:00,500 --> 00:00:04,000",
            "第一条",
            "第二条",
            "",
            "3",
            "00:00:04,000 --> 00:00:04,100",
            "第二条",
            "",
            "4",
            "00:00:04,100 --> 00:00:04,500",
            "第四条",
            "第二条",
            "",
            "5",
            "00:00:04,500 --> 00:00:08,100",
            "第四条",
            "",
        ]
    )


def test_render_danmaku_srt_returns_empty_string_for_invalid_or_empty_xml() -> None:
    assert render_danmaku_srt("", line_count=1) == ""
    assert render_danmaku_srt("<i><d></i>", line_count=1) == ""
