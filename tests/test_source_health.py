"""源健康度学习与调度:store 持久化、评分状态、候选排序、key 派生与搜索看门狗。

注意错误类别须在截断前分类(类别词可能在长错误尾部)。
"""

import json
import time
from pathlib import Path

from atv_player.source_health import (
    HEALTH_BAD,
    HEALTH_GOOD,
    HEALTH_UNKNOWN,
    HEALTH_WARN,
    PLAY_OUTCOME_FAIL,
    PLAY_OUTCOME_SUCCESS,
    SEARCH_OUTCOME_EMPTY,
    SEARCH_OUTCOME_SUCCESS,
    SourceHealthRecord,
    SourceHealthStore,
    health_score,
    health_state,
    is_local_search_key,
    playback_line_health_key,
    rank_candidates,
    run_search_with_health,
    search_health_key,
)


def _store(tmp_path: Path) -> SourceHealthStore:
    return SourceHealthStore(tmp_path / "source-health.json")


def test_store_round_trips_search_and_play(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_search(
        "search:plugin:1",
        SEARCH_OUTCOME_SUCCESS,
        cost_ms=850,
        count=12,
    )
    store.record_play("plugin:1:route:量子", PLAY_OUTCOME_SUCCESS)

    search_record = store.get("search:plugin:1")
    assert search_record is not None
    assert search_record.search_success == 1
    assert search_record.last_search_count == 12
    assert search_record.last_search_cost_ms == 850

    play_record = store.get("plugin:1:route:量子")
    assert play_record is not None
    assert play_record.play_success == 1
    assert play_record.last_play_fail_streak == 0

    # 重开 store(内存副本重建)仍能读到
    reopened = _store(tmp_path)
    assert reopened.get("plugin:1:route:量子") == play_record


def test_store_records_fail_streak_and_categorized_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    long_error = "x" * 300 + " 请求超时"
    for _ in range(3):
        store.record_play("k", PLAY_OUTCOME_FAIL, error=long_error)

    record = store.get("k")
    assert record is not None
    assert record.play_fail == 3
    assert record.last_play_fail_streak == 3
    assert record.last_error.startswith("[timeout]")
    assert len(record.last_error) <= 121

    # 成功一次后连败清零
    store.record_play("k", PLAY_OUTCOME_SUCCESS)
    record = store.get("k")
    assert record is not None
    assert record.last_play_fail_streak == 0


def test_store_empty_result_counts_as_half_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for _ in range(5):
        store.record_search("k", SEARCH_OUTCOME_EMPTY, cost_ms=100, count=0)

    record = store.get("k")
    assert record is not None
    assert record.search_empty == 5
    # 5 次空结果:24*(0-2.5)/9 < 0,应为 WARN 或更差,且不为 GOOD
    assert health_state(record) in {HEALTH_WARN, HEALTH_BAD}


def test_store_purges_records_older_than_ninety_days(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_play("stale", PLAY_OUTCOME_SUCCESS)
    store.record_play("fresh", PLAY_OUTCOME_SUCCESS)
    raw = json.loads((tmp_path / "source-health.json").read_text(encoding="utf-8"))
    raw["stale"]["updated_at"] = int(time.time()) - 91 * 24 * 60 * 60
    (tmp_path / "source-health.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )

    reopened = _store(tmp_path)
    assert reopened.get("stale") is None
    assert reopened.get("fresh") is not None
    # 内存里 stale 仍在,purge 删除并落盘;再开一次也没有 stale
    assert reopened.purge_stale() == 1
    assert _store(tmp_path).get("stale") is None
    assert reopened.purge_stale() == 0


def test_store_tolerates_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "source-health.json"
    path.write_text("{not json", encoding="utf-8")
    store = SourceHealthStore(path)
    store.record_play("k", PLAY_OUTCOME_SUCCESS)
    assert store.get("k") is not None
    assert json.loads(path.read_text(encoding="utf-8"))["k"]["play_success"] == 1



def test_health_score_unknown_without_any_sample() -> None:

    record = SourceHealthRecord(key="k")
    assert health_score(record) == 0.0
    assert health_state(record) == HEALTH_UNKNOWN
    assert health_state(None) == HEALTH_UNKNOWN


def test_health_state_good_and_bad_thresholds() -> None:
    good = SourceHealthRecord(
        key="k",
        play_success=8,
        play_fail=1,
        search_success=6,
        search_fail=0,
        search_empty=0,
        last_search_count=15,
        last_search_cost_ms=700,
    )
    assert health_state(good) == HEALTH_GOOD

    bad = SourceHealthRecord(
        key="k",
        play_success=1,
        play_fail=7,
        last_play_fail_streak=4,
    )
    assert health_state(bad) == HEALTH_BAD


def test_rank_candidates_orders_good_first_bad_last_and_keeps_unknown_order() -> None:
    import tempfile

    store = SourceHealthStore(Path(tempfile.mkdtemp()) / "sh.json")
    for _ in range(5):
        store.record_play("good", PLAY_OUTCOME_SUCCESS)
    for _ in range(6):
        store.record_play("bad", PLAY_OUTCOME_FAIL, error="x")

    ranked = rank_candidates(
        ["bad", "u1", "good", "u2"],
        store,
        key_of=lambda key: key,
    )

    # 好 > 无样本(保原序) > 坏
    assert ranked == ["good", "u1", "u2", "bad"]

    # store 为 None 时原样返回(学习关闭)
    assert rank_candidates(["b", "a"], None, key_of=lambda key: key) == ["b", "a"]


def test_playback_line_health_key_branches() -> None:
    assert (
        playback_line_health_key("spider_plugin", "3", "量子")
        == "plugin:3:route:量子"
    )
    assert playback_line_health_key("spider_plugin", "3", "") == "plugin:3"
    assert (
        playback_line_health_key("browse", "", "", drive_share_key="quark@abc@xw")
        == "drive:quark@abc@xw"
    )
    assert (
        playback_line_health_key("telegram", "", "", vod_id="share-9")
        == "drive:share-9"
    )
    assert (
        playback_line_health_key("bilibili", "", "港台")
        == "builtin:bilibili:route:港台"
    )
    assert playback_line_health_key("youtube", "", "") == "builtin:youtube"


def test_search_health_key_and_local_detection() -> None:
    assert search_health_key("plugin:3") == "search:plugin:3"
    assert is_local_search_key("search:smart:search") is True
    assert is_local_search_key("search:history:global") is True
    assert is_local_search_key("search:plugin:3") is False


def test_run_search_with_health_records_success_count_and_cost() -> None:
    import tempfile

    store = SourceHealthStore(Path(tempfile.mkdtemp()) / "sh.json")

    class Controller:
        # 与真实控制器同形:暴露 search_items 方法而非 __call__,
        # 防止 run_search_with_health 退化回"把控制器当函数调"。
        def search_items(self, keyword: str, page: int) -> tuple[list, int]:
            return [{"id": 1}, {"id": 2}], 1

    items, page_count = run_search_with_health(
        Controller(), "关键词", 1, "search:plugin:1", store
    )

    assert len(items) == 2 and page_count == 1
    record = store.get("search:plugin:1")
    assert record is not None
    assert record.search_success == 1
    assert record.last_search_count == 2
    assert record.last_search_cost_ms >= 0


def test_run_search_with_health_records_empty_and_failure() -> None:
    import tempfile

    store = SourceHealthStore(Path(tempfile.mkdtemp()) / "sh.json")

    class EmptyController:
        def search_items(self, keyword: str, page: int) -> tuple[list, int]:
            return [], 0

    items, _ = run_search_with_health(
        EmptyController(), "k", 1, "search:telegram", store
    )
    assert items == []
    assert store.get("search:telegram").search_empty == 1

    class FailingController:
        def search_items(self, keyword: str, page: int) -> tuple[list, int]:
            raise RuntimeError("网络错误: 无法连接")

    try:
        run_search_with_health(FailingController(), "k", 1, "search:telegram", store)
    except RuntimeError:
        pass
    else:
        raise AssertionError("should re-raise")

    record = store.get("search:telegram")
    assert record.search_empty == 1
    assert record.search_fail == 1
    assert record.last_error.startswith("[network]")


def test_run_search_with_health_watchdog_times_out_hung_source() -> None:
    import tempfile
    import threading

    store = SourceHealthStore(Path(tempfile.mkdtemp()) / "sh.json")
    release = threading.Event()

    class HungController:
        def search_items(self, keyword: str, page: int) -> tuple[list, int]:
            release.wait(timeout=30)
            return [], 0

    try:
        run_search_with_health(
            HungController(),
            "k",
            1,
            "search:plugin:9",
            store,
            timeout_seconds=0.2,
        )
    except TimeoutError:
        pass
    else:
        raise AssertionError("watchdog should raise TimeoutError")
    finally:
        release.set()

    record = store.get("search:plugin:9")
    assert record.search_fail == 1
    assert "超时" in record.last_error


def test_run_search_with_health_skips_watchdog_for_local_sources() -> None:
    import tempfile

    store = SourceHealthStore(Path(tempfile.mkdtemp()) / "sh.json")
    calls: list[tuple[str, int]] = []

    class SlowLocal:
        def search_items(self, keyword: str, page: int) -> tuple[list, int]:
            # 本地源即使超过 timeout 也不应被看门狗打断
            time.sleep(0.3)
            calls.append((keyword, page))
            return [1], 1

    items, page_count = run_search_with_health(
        SlowLocal(), "k", 1, "search:history:global", store, timeout_seconds=0.1
    )

    assert items == [1] and page_count == 1
    # 本地源不记健康
    assert store.get("search:history:global") is None
