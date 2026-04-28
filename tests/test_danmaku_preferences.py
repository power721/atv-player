from pathlib import Path

from atv_player.danmaku.models import DanmakuSeriesPreference
from atv_player.danmaku.preferences import DanmakuSeriesPreferenceStore


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
