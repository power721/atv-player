import json
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
