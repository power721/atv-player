from atv_player.metadata.models import MetadataContext
from atv_player.models import VodItem


def test_metadata_context_infers_anime_category_from_raw_title_suffix() -> None:
    query = MetadataContext(
        vod=VodItem(vod_id="v1", vod_name="仙剑奇侠传叁动漫", vod_year="2025"),
        source_kind="browse",
    ).to_query()

    assert query.title == "仙剑奇侠传叁"
    assert query.year == "2025"
    assert query.category_name == "动漫"


def test_metadata_context_combines_pure_season_media_title_with_vod_name() -> None:
    # 网盘季目录("牧神记/S01")的 media_title 是裸季标记;查询标题组合回
    # 分享标题,否则拿 "S01" 去刮削只会得到别名里恰含该字样的错配
    from atv_player.models import PlayItem

    query = MetadataContext(
        vod=VodItem(vod_id="v1", vod_name="牧神记"),
        source_kind="telegram",
        current_item=PlayItem(
            vod_id="v1",
            title="S01E01.2024.2160p.WEB-DL.H264.AAC.mp4",
            url="http://local/driver/tok",
            media_title="S01",
        ),
    ).to_query()

    assert query.title == "牧神记 S01"


def test_metadata_context_keeps_real_media_title_untouched() -> None:
    from atv_player.models import PlayItem

    query = MetadataContext(
        vod=VodItem(vod_id="v1", vod_name="某合集"),
        source_kind="telegram",
        current_item=PlayItem(
            vod_id="v1",
            title="01.mp4",
            url="http://local/driver/tok",
            media_title="等不到说我爱你",
        ),
    ).to_query()

    assert query.title == "等不到说我爱你"
