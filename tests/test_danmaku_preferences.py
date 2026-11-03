import json
from pathlib import Path
import threading

from atv_player.danmaku.models import DanmakuSeriesPreference
from atv_player.danmaku.preferences import (
    DanmakuSeriesPreferenceStore,
    build_danmaku_episode_key,
)
from atv_player.models import PlayItem


def test_preference_store_round_trip(tmp_path: Path) -> None:
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    pref = DanmakuSeriesPreference(
        series_key="jianlai",
        provider="tencent",
        page_url="https://v.qq.com/x/cover/demo.html",
        title="剑来 第12集",
        updated_at=1770000000,
    )

    store.save(pref)

    loaded = store.load("jianlai")

    assert loaded == pref


def test_preference_store_overwrites_existing_series_key(tmp_path: Path) -> None:
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    store.save(
        DanmakuSeriesPreference(
            series_key="jianlai",
            provider="youku",
            page_url="https://v.youku.com/v_show/id_old.html",
            title="旧结果",
            updated_at=1,
        )
    )

    store.save(
        DanmakuSeriesPreference(
            series_key="jianlai",
            provider="tencent",
            page_url="https://v.qq.com/x/cover/demo.html",
            title="新结果",
            updated_at=2,
        )
    )

    loaded = store.load("jianlai")

    assert loaded is not None
    assert loaded.provider == "tencent"
    assert loaded.page_url.endswith("demo.html")
    assert store.load("missing") is None


def test_preference_store_reads_missing_search_title_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "danmaku-series.json"
    path.write_text(
        json.dumps(
            {
                "jianlai": {
                    "provider": "tencent",
                    "page_url": "https://v.qq.com/x/cover/demo.html",
                    "title": "剑来 第12集",
                    "updated_at": 1770000000,
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = DanmakuSeriesPreferenceStore(path).load("jianlai")

    assert loaded is not None
    assert loaded.search_title == ""


def test_preference_store_round_trip_preserves_search_title(tmp_path: Path) -> None:
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    pref = DanmakuSeriesPreference(
        series_key="jianlai",
        provider="tencent",
        page_url="https://v.qq.com/x/cover/demo.html",
        title="剑来 第12集",
        search_title="剑来",
        updated_at=1770000000,
    )

    store.save(pref)

    loaded = store.load("jianlai")
    payload = json.loads((tmp_path / "danmaku-series.json").read_text(encoding="utf-8"))

    assert loaded == pref
    assert payload["jianlai"]["search_title"] == "剑来"


def test_preference_store_isolates_episode_provider_offsets(tmp_path: Path) -> None:
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")

    store.save_offset("jianlai", "episode:12", "tencent", -3.0)
    store.save_offset("jianlai", "episode:12", "bilibili", 1.5)
    store.save_offset("jianlai", "episode:13", "tencent", 2.0)

    assert store.load_offset("jianlai", "episode:12", "tencent") == -3.0
    assert store.load_offset("jianlai", "episode:12", "bilibili") == 1.5
    assert store.load_offset("jianlai", "episode:13", "tencent") == 2.0


def test_preference_store_zero_removes_offset_and_reads_old_json(tmp_path: Path) -> None:
    path = tmp_path / "danmaku-series.json"
    path.write_text(
        '{"jianlai":{"provider":"tencent","page_url":"u","title":"t","updated_at":1}}',
        encoding="utf-8",
    )
    store = DanmakuSeriesPreferenceStore(path)

    assert store.load_offset("jianlai", "episode:12", "tencent") == 0.0
    store.save_offset("jianlai", "episode:12", "tencent", 4.0)
    store.save_offset("jianlai", "episode:12", "tencent", 0.0)

    assert store.load_offset("jianlai", "episode:12", "tencent") == 0.0
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["jianlai"]["provider"] == "tencent"
    assert payload["jianlai"].get("episode_source_offsets", {}) == {}


def test_preference_store_ignores_invalid_persisted_offsets(tmp_path: Path) -> None:
    path = tmp_path / "danmaku-series.json"
    path.write_text(
        json.dumps(
            {
                "jianlai": {
                    "provider": "tencent",
                    "page_url": "u",
                    "title": "t",
                    "episode_source_offsets": {
                        "episode:12": {
                            "tencent": "nan",
                            "bilibili": 601,
                            "iqiyi": 2.5,
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    store = DanmakuSeriesPreferenceStore(path)

    assert store.load_offset("jianlai", "episode:12", "tencent") == 0.0
    assert store.load_offset("jianlai", "episode:12", "bilibili") == 0.0
    assert store.load_offset("jianlai", "episode:12", "iqiyi") == 2.5


def test_preference_store_concurrent_offset_saves_keep_both_providers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "danmaku-series.json"
    store = DanmakuSeriesPreferenceStore(path)
    barrier = threading.Barrier(3)

    def save(provider: str, value: float) -> None:
        barrier.wait()
        store.save_offset("jianlai", "episode:12", provider, value)

    threads = [
        threading.Thread(target=save, args=("tencent", -2.0)),
        threading.Thread(target=save, args=("bilibili", 1.5)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["jianlai"]["episode_source_offsets"]["episode:12"] == {
        "bilibili": 1.5,
        "tencent": -2.0,
    }


def test_build_danmaku_episode_key_prefers_inferred_playlist_episode() -> None:
    first = PlayItem(title="第11集", url="")
    current = PlayItem(title="第12集", url="")

    assert build_danmaku_episode_key(current, [first, current]) == "episode:12"


def test_build_danmaku_episode_key_uses_normalized_search_label() -> None:
    item = PlayItem(
        title="片头曲",
        url="",
        danmaku_search_episode="  Special Episode  ",
    )

    assert build_danmaku_episode_key(item) == "label:specialepisode"


def test_build_danmaku_episode_key_uses_stable_item_digest() -> None:
    item = PlayItem(
        title="1080p.mkv",
        url="",
        vod_id="stable-item-id",
    )

    assert build_danmaku_episode_key(item) == build_danmaku_episode_key(item)
    assert build_danmaku_episode_key(item).startswith("item:")


def test_build_danmaku_episode_key_falls_back_to_single() -> None:
    item = PlayItem(title="片头曲", url="")

    assert build_danmaku_episode_key(item) == "single"


# ---- 弹幕手动匹配两层记忆(站点级 + TMDB 季级) ----


def test_build_danmaku_tmdb_season_key_validates_identity() -> None:
    from atv_player.danmaku.preferences import build_danmaku_tmdb_season_key

    assert build_danmaku_tmdb_season_key("tv", 76479, 2) == "tmdb:tv:76479:s2"
    assert build_danmaku_tmdb_season_key("TV", "76479", "2") == "tmdb:tv:76479:s2"
    # 电影没有季级身份,不进跨源层。
    assert build_danmaku_tmdb_season_key("movie", 76479, 1) == ""
    assert build_danmaku_tmdb_season_key("tv", 0, 1) == ""
    assert build_danmaku_tmdb_season_key("tv", -5, 1) == ""
    assert build_danmaku_tmdb_season_key("tv", 76479, 0) == ""
    assert build_danmaku_tmdb_season_key("tv", 76479, -1) == ""
    assert build_danmaku_tmdb_season_key("tv", "", "") == ""
    assert build_danmaku_tmdb_season_key("tv", "garbage", "x") == ""


def test_tmdb_season_layer_round_trips_in_same_store_map(tmp_path: Path) -> None:
    # TMDB 季级条目与标题级条目共用同一 JSON map:key 带冒号前缀,天然不撞。
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    store.save(
        DanmakuSeriesPreference(
            series_key="tmdb:tv:76479:s2",
            provider="tencent",
            page_url="https://v.qq.com/s2e1",
            title="牧神记 第二季 1",
            search_title="牧神记 第二季",
        )
    )

    loaded = store.load("tmdb:tv:76479:s2")

    assert loaded is not None
    assert loaded.provider == "tencent"
    assert loaded.search_title == "牧神记 第二季"
    # 标题 key 不受影响
    assert store.load("mushenji") is None


def test_merged_source_preference_tmdb_layer_overrides_series(tmp_path: Path) -> None:
    from atv_player.danmaku.preferences import merged_source_preference

    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    store.save(
        DanmakuSeriesPreference(
            series_key="mushenji",
            provider="youku",
            page_url="https://v.youku.com/s1",
            title="",
            search_title="牧神记",
        )
    )
    store.save(
        DanmakuSeriesPreference(
            series_key="tmdb:tv:76479:s2",
            provider="tencent",
            page_url="https://v.qq.com/s2e1",
            title="",
            search_title="牧神记 第二季",
        )
    )
    item = PlayItem(
        title="S02E05.mkv",
        url="https://media.example/5",
        vod_id="v-1",
        media_title="牧神记 第二季",
        danmaku_series_key="mushenji",
        danmaku_tmdb_season_key="tmdb:tv:76479:s2",
    )

    series_key, preference = merged_source_preference(store, item)

    assert series_key == "mushenji"
    assert preference is not None
    # 季级层覆盖标题层(series_key 剥掉季后缀、跨季共用,季级意图更精确)
    assert preference.provider == "tencent"
    assert preference.page_url == "https://v.qq.com/s2e1"
    assert preference.search_title == "牧神记 第二季"


def test_merged_source_preference_without_overlay_uses_series(tmp_path: Path) -> None:
    from atv_player.danmaku.preferences import merged_source_preference

    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    store.save(
        DanmakuSeriesPreference(
            series_key="mushenji",
            provider="youku",
            page_url="https://v.youku.com/s1",
            title="",
        )
    )
    item = PlayItem(
        title="第1集",
        url="https://media.example/1",
        vod_id="v-1",
        media_title="牧神记",
        danmaku_series_key="mushenji",
        danmaku_tmdb_season_key="",  # 身份不可信:不参与跨源层
    )

    series_key, preference = merged_source_preference(store, item)

    assert series_key == "mushenji"
    assert preference is not None
    assert preference.provider == "youku"


def test_save_tmdb_season_overlay_writes_manual_intent_only_fields(tmp_path: Path) -> None:
    from atv_player.danmaku.preferences import save_tmdb_season_overlay

    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    # tmdb 层已有偏移,覆写选择时必须保留
    store.save(
        DanmakuSeriesPreference(
            series_key="tmdb:tv:76479:s2",
            provider="old",
            page_url="https://old",
            title="",
            episode_source_offsets={"s2:episode:1": {"tencent": -3.0}},
        )
    )
    item = PlayItem(
        title="S02E01.mkv",
        url="https://media.example/1",
        vod_id="v-1",
        media_title="牧神记 第二季",
        danmaku_series_key="mushenji",
        danmaku_tmdb_season_key="tmdb:tv:76479:s2",
    )
    series_preference = DanmakuSeriesPreference(
        series_key="mushenji",
        provider="tencent",
        page_url="https://v.qq.com/s2e1",
        title="牧神记 第二季 1",
        search_title="牧神记 第二季",
        episode_source_offsets={"s1:episode:1": {"youku": 5.0}},
    )
    store.save(series_preference)

    save_tmdb_season_overlay(store, item, series_preference)

    overlay = store.load("tmdb:tv:76479:s2")
    assert overlay is not None
    assert overlay.provider == "tencent"
    assert overlay.page_url == "https://v.qq.com/s2e1"
    assert overlay.search_title == "牧神记 第二季"
    assert overlay.episode_source_offsets == {"s2:episode:1": {"tencent": -3.0}}
    # series 层原样,不带入 tmdb 层的偏移
    series = store.load("mushenji")
    assert series is not None
    assert series.episode_source_offsets == {"s1:episode:1": {"youku": 5.0}}


def test_save_tmdb_season_overlay_skips_without_trusted_identity(tmp_path: Path) -> None:
    from atv_player.danmaku.preferences import save_tmdb_season_overlay

    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    item = PlayItem(title="第1集", url="https://media.example/1", vod_id="v-1")
    preference = DanmakuSeriesPreference(
        series_key="mushenji", provider="tencent", page_url="https://v.qq.com/1", title=""
    )

    save_tmdb_season_overlay(store, item, preference)

    assert store.load("tmdb:tv:76479:s2") is None


def test_build_danmaku_episode_key_prefixes_season_when_known() -> None:
    item = PlayItem(title="S02E05 神魔剑.mkv", url="https://media.example/5", vod_id="v-1")
    playlist = [PlayItem(title=f"S02E{ep:02d}.mkv", url=f"https://media.example/{ep}") for ep in range(1, 6)]
    playlist[4] = item

    assert build_danmaku_episode_key(item, playlist) == "s2:episode:5"

    plain = PlayItem(title="第5集.mkv", url="https://media.example/5", vod_id="v-1")
    plain_playlist = [PlayItem(title=f"第{ep}集.mkv", url=f"https://media.example/{ep}") for ep in range(1, 6)]
    plain_playlist[4] = plain

    assert build_danmaku_episode_key(plain, plain_playlist) == "episode:5"


def test_load_item_danmaku_offset_falls_back_to_legacy_episode_key(tmp_path: Path) -> None:
    from atv_player.danmaku.preferences import load_item_danmaku_offset

    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    # 老版本写入的无季前缀 key
    store.save_offset("mushenji", "episode:5", "tencent", -4.0)
    item = PlayItem(
        title="S02E05.mkv",
        url="https://media.example/5",
        vod_id="v-1",
        media_title="牧神记 第二季",
        danmaku_series_key="mushenji",
        selected_danmaku_provider="tencent",
    )
    playlist = [PlayItem(title=f"S02E{ep:02d}.mkv", url=f"https://media.example/{ep}") for ep in range(1, 6)]
    playlist[4] = item

    assert load_item_danmaku_offset(store, item, playlist) == -4.0

    # 季感知 key 存在时优先于 legacy
    store.save_offset("mushenji", "s2:episode:5", "tencent", 2.5)

    assert load_item_danmaku_offset(store, item, playlist) == 2.5
