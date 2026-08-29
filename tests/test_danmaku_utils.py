from atv_player.danmaku.models import (
    DanmakuRecord,
    DanmakuSourceGroup,
    DanmakuSourceOption,
    DanmakuSourceSearchResult,
)
from atv_player.danmaku.utils import (
    build_xml,
    episode_matches_request,
    extract_episode_number,
    extract_official_link_url,
    extract_part_number,
    extract_variety_episode_label,
    extract_variety_issue_key,
    extract_variety_part,
    has_explicit_episode_marker,
    has_variety_issue_marker,
    infer_playlist_episode_number,
    is_likely_variety_title,
    is_other_fallback_only_result,
    is_stale_other_fallback_result,
    is_variety_collection,
    match_provider,
    normalize_name,
    should_filter_name,
    strip_episode_suffix,
    strip_variety_issue_suffix,
)
from atv_player.models import (
    PlaybackDetailField,
    PlaybackDetailFieldAction,
    PlaybackDetailValuePart,
    PlayItem,
)


def test_normalize_name_strips_noise_tokens() -> None:
    assert normalize_name(" 剑来 第1集【高清】(qq.com) ") == "剑来 第1集"


def test_match_provider_maps_known_domains() -> None:
    assert match_provider("https://v.qq.com/x/cover/demo.html") == "tencent"
    assert match_provider("https://v.youku.com/v_show/id_demo.html") == "youku"
    assert match_provider("https://www.iqiyi.com/v_demo.html") == "iqiyi"
    assert match_provider("https://www.mgtv.com/b/demo.html") == "mgtv"
    assert match_provider("https://example.com/watch/1") is None


def test_should_filter_name_rejects_unrelated_titles() -> None:
    target = normalize_name("剑来 第1集")
    assert should_filter_name(target, "凡人修仙传 第1集") is True
    assert should_filter_name(target, "剑来 第1集") is False


def test_should_filter_name_rejects_sequel_mismatch() -> None:
    target = normalize_name("疯狂动物城2")
    assert should_filter_name(target, "疯狂动物城") is True
    assert should_filter_name(target, "疯狂动物城2（普通话版）") is False


def test_should_filter_name_accepts_season_number_format_variants() -> None:
    target = normalize_name("哈哈哈哈哈第六季")
    assert should_filter_name(target, "哈哈哈哈哈第6季 第1期上 邓超陈赫癫狂式唱山歌") is False


def test_should_filter_name_keeps_marker_led_episode_subtitle() -> None:
    # Tencent-style "第N集 <剧情>" carries no show name; it must not be dropped by
    # name-similarity against the show query (regression: 《百花杀》第13集 搜不到).
    target = normalize_name("百花杀 13集")
    assert should_filter_name(target, "第十三集 朱字当众揭穿我藏在凤椅下，皇后一句谁先取她的命") is False


def test_should_filter_name_keeps_date_led_variety_episode_subtitle() -> None:
    # Tencent names variety episodes by air date + 第N期 + subtitle, with no show
    # name (e.g. "2026-08-10 第2期上：<subtitle>"); such candidates must not be
    # dropped by name-similarity (regression: 《心动的信号 第9季》综艺 0 候选).
    target = normalize_name("心动的信号 第9季 20260810 第2期上")
    assert should_filter_name(target, "2026-08-10 第2期上：偶像剧现场 情歌对唱甜蜜升温") is False


def test_episode_matches_request_rejects_different_show_with_same_episode_number() -> None:
    # A candidate carrying a different show-name prefix must still be rejected even
    # when its episode number equals the request (guard against over-relaxation).
    assert episode_matches_request("隆行天下之重走八千里路云和月 第16集", 16, "八千里路云和月 16集") is False


def test_extract_episode_number_supports_numeric_title_with_size_suffix() -> None:
    assert extract_episode_number("12(1.26 GB)") == 12


def test_extract_episode_number_supports_chinese_numerals() -> None:
    assert extract_episode_number("第十二集") == 12


def test_extract_episode_number_prefers_explicit_episode_over_part_suffix() -> None:
    assert extract_episode_number("第25集 神魔剑（下）") == 25


def test_extract_episode_number_maps_movie_part_markers() -> None:
    # 电影分上中下:分部标记映射 1/2/3(腾讯真实片源标题, cid mzc00200s0ntzpo)
    assert extract_episode_number("上集：喜迁新居，竟遇“诡”邻") == 1
    assert extract_episode_number("中集：双面丈夫，究竟谁在说谎？") == 2
    assert extract_episode_number("下集：终极反转！全员恶人互搏") == 3
    assert extract_episode_number("流浪地球上集") == 1
    assert extract_episode_number("X电影 中部") == 2
    assert extract_episode_number("X电影 下篇 1080p") == 3
    assert extract_episode_number("X电影（上）") == 1
    assert extract_episode_number("X电影 上") == 1
    assert extract_episode_number("上部") == 1
    assert extract_part_number("X电影 上卷") == 1


def test_extract_episode_number_ignores_part_marker_false_positives() -> None:
    # 裸"上/中/下"两侧必须是非汉字边界;带单位时单位后不能接汉字
    assert extract_episode_number("上海堡垒") is None
    assert extract_episode_number("碟中谍") is None
    assert extract_episode_number("史上最强") is None
    assert extract_episode_number("网上集结") is None
    assert extract_episode_number("上部城市") is None
    assert extract_episode_number("X电影 中英双字") is None
    assert extract_episode_number("午夜凶铃 上映版") is None
    # "期上/期 上"是综艺分部(第2期上),归 variety 逻辑,集数仍是期号
    assert extract_episode_number("奔跑吧 第2期上") == 2
    assert extract_episode_number("2026-08-10 第2期 上") == 2


def test_strip_episode_suffix_strips_part_markers() -> None:
    assert strip_episode_suffix("电影 上集") == "电影"
    assert strip_episode_suffix("流浪地球上集") == "流浪地球"
    assert strip_episode_suffix("电影（下）") == "电影"
    assert strip_episode_suffix("电影 上") == "电影"
    # 组合查询叠了数字集标也能剥干净
    assert strip_episode_suffix("电影 上集 1集") == "电影"


def test_has_explicit_episode_marker_recognizes_part_markers() -> None:
    assert has_explicit_episode_marker("电影 上集") is True
    assert has_explicit_episode_marker("电影 上") is True
    assert has_explicit_episode_marker("上海堡垒") is False
    assert has_explicit_episode_marker("碟中谍") is False


def test_episode_matches_request_accepts_part_markered_movie_episodes() -> None:
    # 分部命名的候选("上集：副标题")以分部开头,无剧名前缀,按集数匹配
    assert episode_matches_request("上集：喜迁新居，竟遇“诡”邻", 1, "诡邻 1集") is True
    assert episode_matches_request("中集：双面丈夫，究竟谁在说谎？", 2, "诡邻 2集") is True
    assert episode_matches_request("下集：终极反转！全员恶人互搏", 1, "诡邻 1集") is False


def test_episode_matches_request_accepts_subtitle_only_episode_name() -> None:
    # Tencent names episodes by plot subtitle without the show name ("第十三集 <剧情>");
    # the number match alone must count as a match for an explicit episode request.
    assert episode_matches_request("第十三集 朱字当众揭穿我藏在凤椅下", 13, "百花杀 13集") is True


def test_episode_matches_request_accepts_show_prefixed_episode_name() -> None:
    assert episode_matches_request("百花杀 第13集", 13, "百花杀 13集") is True


def test_episode_matches_request_rejects_wrong_episode_number() -> None:
    assert episode_matches_request("第十二集 剧情字幕", 13, "百花杀 13集") is False


def test_episode_matches_request_accepts_plot_subtitle_numbering_after_episode_marker() -> None:
    # Bilibili names episodes "第138话 外海风云14"; the subtitle's trailing number
    # is plot numbering, not a sequel marker, so the correct episode must match
    # (regression: 《凡人修仙传》138 集预下载到 136 话的弹幕).
    assert episode_matches_request("《凡人修仙传》第138话 外海风云14", 138, "凡人修仙传 138集") is True
    assert episode_matches_request("《凡人修仙传》第136话 外海风云12", 138, "凡人修仙传 138集") is False


def test_episode_matches_request_keeps_rejecting_season_mismatch_with_plot_subtitle() -> None:
    assert episode_matches_request("《凡人修仙传 第二季》第2话 外海风云14", 2, "凡人修仙传 第2季 2集") is True
    assert episode_matches_request("《凡人修仙传》第2话 外海风云14", 2, "凡人修仙传 第2季 2集") is False


def test_extract_episode_number_supports_zero_padded_prefix_titles() -> None:
    assert extract_episode_number("0002 剑来-笼中雀") == 2


def test_extract_variety_issue_key_supports_calendar_issue_titles() -> None:
    assert extract_variety_issue_key("你好星期六 20250104期") == "20250104"


def test_extract_variety_part_finds_half_marker_after_issue() -> None:
    assert extract_variety_part("2026-08-10 第2期上：偶像剧现场") == "上"
    assert extract_variety_part("哈哈哈哈哈第6季 第1期下 邓超陈赫斗舞") == "下"
    assert extract_variety_part("第1期加更上 小屋荡秋千") == "加更上"
    assert extract_variety_part("第1期加更") == "加更"


def test_extract_variety_part_returns_none_without_marker() -> None:
    assert extract_variety_part("你好星期六 20250104期") is None
    assert extract_variety_part("歌手2026 第12期") is None
    # 完整版 must NOT be read as part 完 (followed by CJK 整).
    assert extract_variety_part("《哈哈哈哈哈第六季》 第七期 完整版") is None


def test_extract_variety_issue_key_appends_part_for_disambiguation() -> None:
    assert extract_variety_issue_key("2026-08-10 第2期上：偶像剧现场") == "20260810上"
    assert extract_variety_issue_key("2026-08-10 第2期中：首次约会") == "20260810中"
    assert extract_variety_issue_key("2026-08-11 第2期下：勇敢者约会") == "20260811下"
    # No part -> bare key (back-compat with calendar issue titles).
    assert extract_variety_issue_key("你好星期六 20250104期") == "20250104"


def test_extract_variety_episode_label_builds_from_filename() -> None:
    assert extract_variety_episode_label("2026.08.10-第2期上.mp4") == "20260810 第2期上"
    assert extract_variety_episode_label("第2期上.mp4") == "第2期上"
    assert extract_variety_episode_label("第1期加更上.mp4") == "第1期加更上"
    assert extract_variety_episode_label("纯享版.mp4") == ""


def test_is_variety_collection_reads_metadata_genres() -> None:
    assert is_variety_collection("真人秀") is True
    assert is_variety_collection("综艺") is True
    assert is_variety_collection("", "脱口秀") is True
    assert is_variety_collection("Variety") is True
    assert is_variety_collection("电视剧") is False
    assert is_variety_collection("", "") is False
    # tag/content are also consulted (parity with app-level playlist detection).
    assert is_variety_collection("", "", "", "国内综艺first") is True


def test_has_variety_issue_marker_ignores_bare_dates() -> None:
    # 第N期/N期 and hint tokens qualify...
    assert has_variety_issue_marker("2026-08-10 第2期上") is True
    assert has_variety_issue_marker("20250104期") is True
    assert has_variety_issue_marker("第1期加更") is True
    # ...but a bare air date must NOT, or ordinary episode files with dates
    # (e.g. anime "04-第4话…-2026-03-03") would be misread as variety issues.
    assert has_variety_issue_marker("04-第4话 啥！啥！-1080P-2026-03-03") is False
    assert has_variety_issue_marker("2026.08.10.mp4") is False


def test_match_provider_maps_sohu_and_migu() -> None:
    assert match_provider("https://tv.sohu.com/v/abc.html") == "sohu"
    assert match_provider("https://www.miguvideo.com/p/detail/abc") == "migu"


def test_extract_official_link_url_returns_first_known_platform_link() -> None:
    fields = [
        PlaybackDetailField(label="别名", value="Heart Signal"),
        PlaybackDetailField(
            label="官方链接",
            value_parts=[
                PlaybackDetailValuePart(
                    label="腾讯视频",
                    action=PlaybackDetailFieldAction(
                        type="link", value="https://v.qq.com/x/cover/mzc002003kpyd2m/w4102gzejm3.html"
                    ),
                ),
                PlaybackDetailValuePart(
                    label="爱奇艺",
                    action=PlaybackDetailFieldAction(type="link", value="https://www.iqiyi.com/v_demo.html"),
                ),
            ],
        ),
    ]

    assert extract_official_link_url(fields) == "https://v.qq.com/x/cover/mzc002003kpyd2m/w4102gzejm3.html"


def test_extract_official_link_url_skips_unknown_and_missing_links() -> None:
    assert extract_official_link_url([]) == ""
    assert extract_official_link_url(None) == ""
    # A 官方链接 pointing at an unrecognized host yields nothing to pin.
    unknown = [
        PlaybackDetailField(
            label="官方链接",
            value_parts=[
                PlaybackDetailValuePart(
                    label="其他",
                    action=PlaybackDetailFieldAction(type="link", value="https://example.com/watch/1"),
                )
            ],
        )
    ]
    assert extract_official_link_url(unknown) == ""
    # Plain-text 官方链接 (no action) yields nothing.
    text_only = [PlaybackDetailField(label="官方链接", value="腾讯视频")]
    assert extract_official_link_url(text_only) == ""


def test_is_likely_variety_title_distinguishes_issue_from_episode_titles() -> None:
    assert is_likely_variety_title("歌手2026 第12期") is True
    assert is_likely_variety_title("剑来 第12集") is False


def test_strip_variety_issue_suffix_keeps_base_title() -> None:
    assert strip_variety_issue_suffix("你好星期六 20250104期") == "你好星期六"
    assert strip_variety_issue_suffix("你好星期六 2025-01-04") == "你好星期六"
    assert strip_variety_issue_suffix("哈哈哈哈哈第六季 20260404期 第1期上：最狠开局！五哈团命悬一线好刺激 4K60") == "哈哈哈哈哈第六季"


def test_extract_episode_number_supports_cjk_bar_separated_prefix_titles() -> None:
    assert extract_episode_number("01丨4K.mp4") == 1


def test_extract_episode_number_prefers_trailing_episode_over_range_prefix() -> None:
    assert extract_episode_number("01-99集 - 39(147.67 MB)") == 39


def test_extract_episode_number_ignores_range_only_prefix_titles() -> None:
    assert extract_episode_number("01-99集") is None


def test_extract_episode_number_ignores_complete_series_count_marker() -> None:
    assert extract_episode_number("24集全") is None


def test_extract_episode_number_supports_quality_variant_prefix_titles() -> None:
    assert extract_episode_number("160-4K.mp4(471.43 MB)") == 160


def test_extract_episode_number_supports_tilde_quality_variant_prefix_titles() -> None:
    assert extract_episode_number("04~4K.HQ.HEVC(2.1 GB)") == 4


def test_extract_episode_number_supports_labeled_tilde_quality_variant_titles() -> None:
    assert extract_episode_number("高码率 - 04~4K.HQ.HEVC(2.1 GB)") == 4


def test_extract_episode_number_ignores_numeric_x_suffix_filenames() -> None:
    assert extract_episode_number("70x.mp4(2.52 GB)") is None
    assert extract_episode_number("78X.mkv(2.89 GB)") is None
    assert extract_episode_number("82 x.mp4(1.67 GB)") is None


def test_infer_playlist_episode_number_prefers_clean_path_over_date_like_title_suffix() -> None:
    item = PlayItem(
        title="逆丨天邪神 (2023) - 01-4K-[H265.AAC][2023-09-23(815.88 MB)",
        url="http://m/1.mp4",
        path="/show/01-4K-[H265.AAC][2023-09-23].mp4",
        index=0,
    )

    assert infer_playlist_episode_number(item, [item]) == 1


def test_infer_playlist_episode_number_prefers_current_title() -> None:
    playlist = [
        PlayItem(title="0001 剑来-总管坐镇剑气长城", url="http://m/1.mp4", index=0),
        PlayItem(title="0002 剑来-笼中雀", url="http://m/2.mp4", index=1),
        PlayItem(title="0003 剑来-第三集", url="http://m/3.mp4", index=2),
    ]

    assert infer_playlist_episode_number(playlist[1], playlist) == 2


def test_infer_playlist_episode_number_prefers_trailing_episode_over_range_prefix() -> None:
    playlist = [
        PlayItem(title="01-08 - 01(1.66 GB)", url="http://m/1.mp4", index=0),
        PlayItem(title="01-08 - 02(1.54 GB)", url="http://m/2.mp4", index=1),
        PlayItem(title="01-08 - 03(1.42 GB)", url="http://m/3.mp4", index=2),
    ]

    assert infer_playlist_episode_number(playlist[1], playlist) == 2


def test_infer_playlist_episode_number_prefers_trailing_episode_over_complete_series_count() -> None:
    playlist = [
        PlayItem(title="第一季 1080P 6集全 - 01(3.67 GB)", url="http://m/1.mp4", index=0),
        PlayItem(title="第二季 1080P 6集全 - 02(2.76 GB)", url="http://m/2.mp4", index=1),
        PlayItem(title="第一季 1080P 6集全 - 06(3.68 GB)", url="http://m/6.mp4", index=2),
    ]

    assert infer_playlist_episode_number(playlist[0], playlist) == 1
    assert infer_playlist_episode_number(playlist[1], playlist) == 2
    assert infer_playlist_episode_number(playlist[2], playlist) == 6


def test_infer_playlist_episode_number_prefers_cjk_bar_separated_prefix_titles_over_playlist_position() -> None:
    playlist = [
        PlayItem(title="01~4K.mp4", url="http://m/1.mp4", index=0),
        PlayItem(title="01丨4K.mp4", url="http://m/1b.mp4", index=1),
        PlayItem(title="02丨4K.mp4", url="http://m/2.mp4", index=2),
        PlayItem(title="03-4K.mp4", url="http://m/3.mp4", index=3),
    ]

    assert infer_playlist_episode_number(playlist[1], playlist) == 1
    assert infer_playlist_episode_number(playlist[2], playlist) == 2


def test_infer_playlist_episode_number_falls_back_to_playlist_position() -> None:
    playlist = [
        PlayItem(title="正片.mp4", url="http://m/1.mp4", index=0),
        PlayItem(title="国语.mp4", url="http://m/2.mp4", index=1),
        PlayItem(title="超清.mp4", url="http://m/3.mp4", index=2),
    ]

    assert infer_playlist_episode_number(playlist[1], playlist) == 2


def test_infer_playlist_episode_number_ignores_year_prefixed_media_filename() -> None:
    playlist = [
        PlayItem(
            title="2025.2160p.iTunes.WEB-DL.H265.DV.HDR.DDP5.1.Atmos.mkv(18.87 GB)",
            url="http://m/1.mp4",
            index=0,
        ),
        PlayItem(
            title="Zootopia.2.2025.1080p.AMZN.WEB-DL.English.DDP5.1.H.264.mkv(5.51 GB)",
            url="http://m/2.mp4",
            index=1,
        ),
    ]

    assert infer_playlist_episode_number(playlist[0], playlist) is None


def test_infer_playlist_episode_number_ignores_opening_and_ending_bonus_titles() -> None:
    playlist = [
        PlayItem(
            title="片头片尾 - 片头《弑神》大志Tiger [2025-12-28].mp4(64.87 MB)",
            url="http://m/op.mp4",
            index=0,
        ),
        PlayItem(
            title="片头片尾 - 片尾《归无》段奥娟 [202512-28].mp4(59.03 MB)",
            url="http://m/ed.mp4",
            index=1,
        ),
    ]

    assert infer_playlist_episode_number(playlist[0], playlist) is None
    assert infer_playlist_episode_number(playlist[1], playlist) is None


def test_infer_playlist_episode_number_uses_playlist_position_for_numeric_x_suffix_filenames() -> None:
    playlist = [
        PlayItem(title="70x.mp4(2.52 GB)", url="http://m/70.mp4", index=0),
        PlayItem(title="71x.mp4(2.57 GB)", url="http://m/71.mp4", index=1),
        PlayItem(title="78X.mkv(2.89 GB)", url="http://m/78.mkv", index=2),
        PlayItem(title="82 x.mp4(1.67 GB)", url="http://m/82.mp4", index=3),
        PlayItem(title="83 x.mp4(2.94 GB)", url="http://m/83.mp4", index=4),
    ]

    assert infer_playlist_episode_number(playlist[0], playlist) == 1
    assert infer_playlist_episode_number(playlist[1], playlist) == 2
    assert infer_playlist_episode_number(playlist[2], playlist) == 3
    assert infer_playlist_episode_number(playlist[3], playlist) == 4
    assert infer_playlist_episode_number(playlist[4], playlist) == 5


def test_infer_playlist_episode_number_uses_actual_playlist_position_when_item_index_is_unset() -> None:
    playlist = [
        PlayItem(title="70x.mp4(2.52 GB)", url="http://m/70.mp4"),
        PlayItem(title="71x.mp4(2.57 GB)", url="http://m/71.mp4"),
        PlayItem(title="82 x.mp4(1.67 GB)", url="http://m/82.mp4"),
    ]

    assert infer_playlist_episode_number(playlist[0], playlist) == 1
    assert infer_playlist_episode_number(playlist[1], playlist) == 2
    assert infer_playlist_episode_number(playlist[2], playlist) == 3


def test_infer_playlist_episode_number_falls_back_to_path_when_display_title_hides_numeric_filename() -> None:
    playlist = [
        PlayItem(
            title="The.Boys.S05E06(8.5 GB)",
            url="http://m/6.mp4",
            path="/show/Season5/S05E06.2160p.AMZN.WEB-DL.DDP5.1.Atmos.HDR10P.H.265.mkv",
            index=0,
        ),
        PlayItem(
            title="4K内嵌中英双语 - 1.mp4(3.46 GB)",
            url="http://m/1.mp4",
            path="/show/Season5/4K内嵌中英双语/1.mp4",
            index=1,
        ),
    ]

    assert infer_playlist_episode_number(playlist[1], playlist) == 1


def test_build_xml_escapes_content_and_keeps_expected_shape() -> None:
    xml = build_xml(
        [
            DanmakuRecord(time_offset=1.5, pos=1, color="16777215", content="a < b & c"),
            DanmakuRecord(time_offset=3.0, pos=4, color="255", content='"quoted"'),
        ]
    )

    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?><i>')
    assert '<d p="1.5,1,25,16777215">a &lt; b &amp; c</d>' in xml
    assert '<d p="3.0,4,25,255">"quoted"</d>' in xml
    assert xml.endswith("</i>")


def test_build_xml_strips_xml_illegal_control_characters() -> None:
    # A danmaku carrying an XML-1.0-illegal control char (\x08) must not corrupt the
    # whole document — otherwise the entire payload fails to parse and renders empty
    # (regression: 12万+弹幕因单条含 \x08 导致 "弹幕加载失败: 弹幕为空").
    from xml.etree import ElementTree

    xml = build_xml([DanmakuRecord(time_offset=1.0, pos=1, color="16777215", content="护照上\x08不是吗")])

    root = ElementTree.fromstring(xml)  # must not raise
    nodes = root.findall(".//d")
    assert len(nodes) == 1
    assert nodes[0].text == "护照上不是吗"


def test_play_item_episode_number_ignores_spider_site_id_paths() -> None:
    from atv_player.danmaku.utils import _play_item_episode_number

    # 回归：网盘占位条目把站点详情 ID 存在 path 里，"9794.html" 的页面编号被当成集数，
    # 搜索词变成 "爱是愤怒 9794集"。
    item = PlayItem(
        title="百度",
        url="https://pan.baidu.com/s/1demo?pwd=9527",
        media_title="爱是愤怒",
        path="site:duoduo:/index.php/vod/detail/id/9794.html",
    )

    assert _play_item_episode_number(item) is None


def test_play_item_episode_number_ignores_web_page_basenames_in_path() -> None:
    from atv_player.danmaku.utils import _play_item_episode_number

    item = PlayItem(title="百度", url="https://pan.baidu.com/s/1demo", path="/cache/detail/id/1204.html")

    assert _play_item_episode_number(item) is None


def test_play_item_episode_number_keeps_media_url_path_basenames() -> None:
    from atv_player.danmaku.utils import _play_item_episode_number

    item = PlayItem(title="选集", url="https://m.example/1.m3u8", path="https://cdn.example/show/EP05.mp4")

    assert _play_item_episode_number(item) == 5


def test_infer_playlist_episode_number_falls_back_to_position_for_site_id_path() -> None:
    playlist = [
        PlayItem(
            title="百度",
            url="https://pan.baidu.com/s/1demo",
            path="site:duoduo:/index.php/vod/detail/id/9794.html",
            index=0,
        ),
        PlayItem(
            title="夸克",
            url="https://pan.quark.cn/s/_demo",
            path="site:duoduo:/index.php/vod/detail/id/9794.html",
            index=1,
        ),
    ]

    # 修复前：第二项从 path 的 "9794.html" 解析出集数 9794。
    assert infer_playlist_episode_number(playlist[1], playlist) == 2


def _source_search_result(provider: str, url: str) -> DanmakuSourceSearchResult:
    return DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider=provider,
                provider_label=provider,
                options=[DanmakuSourceOption(provider=provider, name="冷门剧 1集", url=url)],
            )
        ],
        default_option_url=url,
        default_provider=provider,
    )


def test_is_other_fallback_only_result_detects_synthetic_candidate() -> None:
    assert is_other_fallback_only_result(_source_search_result("other", "https://v.qq.com/x/1")) is True
    assert is_other_fallback_only_result(_source_search_result("tencent", "https://v.qq.com/x/1")) is False
    assert is_other_fallback_only_result(DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")) is False


def test_is_stale_other_fallback_result_flags_foreign_reg_src_entries() -> None:
    poisoned = _source_search_result("other", "https://pan.baidu.com/s/1demo?pwd=9527")

    # 候选 URL 指向别的 reg_src（含系列级缓存里 reg_src 为空的情形）→ 过期污染，拒用。
    assert is_stale_other_fallback_result(poisoned, "/我的百度分享/爱是愤怒/file.mp4") is True
    assert is_stale_other_fallback_result(poisoned, "") is True
    # 候选 URL 与当前 reg_src 一致 → 自洽条目，保留。
    assert is_stale_other_fallback_result(poisoned, "https://pan.baidu.com/s/1demo?pwd=9527") is False
    assert is_stale_other_fallback_result(_source_search_result("tencent", "https://v.qq.com/x/1"), "") is False
