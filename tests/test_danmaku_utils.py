from atv_player.danmaku.models import DanmakuRecord
from atv_player.danmaku.utils import (
    build_xml,
    extract_episode_number,
    match_provider,
    normalize_name,
    should_filter_name,
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


def test_extract_episode_number_supports_numeric_title_with_size_suffix() -> None:
    assert extract_episode_number("12(1.26 GB)") == 12


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
