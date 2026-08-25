from __future__ import annotations

import threading

from atv_player.models import HistoryRecord
from atv_player.playback_sync_service import (
    INITIAL_DELAY_MS,
    MAX_IDLE_PUSHES,
    PULL_PERIOD_MS,
    PULL_SOON_MIN_INTERVAL_MS,
    PERIOD_MS,
    PlaybackHistorySyncService,
)


def _record(
    *,
    key: str,
    source_key: str = "",
    source_kind: str = "emby",
    source_name: str = "",
    updated_at: int,
    position: int = 100,
) -> HistoryRecord:
    return HistoryRecord(
        id=0,
        key=key,
        vod_name=key,
        vod_pic="",
        vod_remarks="",
        episode=1,
        episode_url="url",
        position=position,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=updated_at,
        source_kind=source_kind,
        source_key=source_key,
        source_name=source_name,
    )


class FakeRepository:
    def __init__(self, records: list[HistoryRecord]) -> None:
        self.records = {
            (record.source_kind, record.source_key, record.key): record
            for record in records
        }
        self.deleted: list[tuple[str, str, str]] = []
        self.saved: list[tuple[str, str, str]] = []
        self.saved_source_names: list[str] = []
        self.saved_payloads: list[dict] = []
        self.cursors: dict[str, int] = {}
        self.snapshots: dict[str, dict[tuple[str, str, str], int]] = {}
        self.pending_deletions: dict[tuple[str, str, str], int] = {}

    def list_histories(self) -> list[HistoryRecord]:
        return list(self.records.values())

    def get_history(self, source_kind: str, vod_id: str, source_key: str = ""):
        return self.records.get((source_kind, source_key, vod_id))

    def save_history(
        self,
        source_kind: str,
        vod_id: str,
        payload: dict,
        *,
        source_key: str = "",
        source_name: str = "",
    ) -> None:
        self.saved.append((source_kind, source_key, vod_id))
        self.saved_source_names.append(source_name)
        self.saved_payloads.append(dict(payload))

    def delete_history(
        self, source_kind: str, vod_id: str, source_key: str = ""
    ) -> None:
        self.deleted.append((source_kind, source_key, vod_id))
        self.records.pop((source_kind, source_key, vod_id), None)

    def delete_site_history(
        self, source_kind: str, source_key: str, deleted_at: int
    ) -> list[tuple[str, str, str]]:
        removed = [
            identity
            for identity, record in self.records.items()
            if identity[:2] == (source_kind, source_key)
            and (deleted_at <= 0 or record.create_time <= deleted_at)
        ]
        for identity in removed:
            self.records.pop(identity)
            self.deleted.append(identity)
        return removed

    def delete_all_histories(self, deleted_at: int) -> list[tuple[str, str, str]]:
        removed = [
            identity
            for identity, record in self.records.items()
            if deleted_at <= 0 or record.create_time <= deleted_at
        ]
        for identity in removed:
            self.records.pop(identity)
            self.deleted.append(identity)
        return removed

    def get_sync_cursor(self, namespace: str) -> int:
        return self.cursors.get(namespace, 0)

    def set_sync_cursor(self, namespace: str, cursor: int) -> None:
        self.cursors[namespace] = cursor

    def load_sync_snapshot(self, namespace: str) -> dict[tuple[str, str, str], int]:
        return dict(self.snapshots.get(namespace, {}))

    def replace_sync_snapshot(
        self, namespace: str, versions: dict[tuple[str, str, str], int]
    ) -> None:
        self.snapshots[namespace] = dict(versions)

    def set_sync_snapshot_version(
        self,
        namespace: str,
        identity: tuple[str, str, str],
        updated_at: int,
    ) -> None:
        self.snapshots.setdefault(namespace, {})[identity] = updated_at

    def remove_sync_snapshot(
        self, namespace: str, identity: tuple[str, str, str]
    ) -> None:
        self.snapshots.setdefault(namespace, {}).pop(identity, None)

    def record_pending_deletion(
        self, source_kind: str, source_key: str, vod_id: str, deleted_at: int
    ) -> None:
        self.pending_deletions[(source_kind, source_key, vod_id)] = int(deleted_at)

    def list_pending_deletions(self) -> list[tuple[str, str, str, int]]:
        return [
            (source_kind, source_key, vod_id, deleted_at)
            for (source_kind, source_key, vod_id), deleted_at in self.pending_deletions.items()
        ]

    def clear_pending_deletions(self, items: list[tuple[str, str, str]]) -> None:
        for identity in items:
            self.pending_deletions.pop(identity, None)


class FakeApi:
    def __init__(self, page: dict | None = None) -> None:
        self.page = page or {}
        self.pushed: list[list[dict]] = []
        self.pull_source_kinds = ""
        self.pull_site_keys = ""
        self.pull_since: list[int] = []
        self.playback_sync_identity = "test-user"

    def push_playback_events(self, records: list[dict]) -> None:
        self.pushed.append(records)

    def pull_playback_records(
        self, since: int, *, source_kinds: str = "", site_keys: str = ""
    ) -> dict:
        self.pull_since.append(since)
        self.pull_source_kinds = source_kinds
        self.pull_site_keys = site_keys
        return self.page


def test_playback_sync_runs_every_30_seconds() -> None:
    assert INITIAL_DELAY_MS == 30_000
    assert PERIOD_MS == 30_000
    # PULL 与安卓端 60s 同步周期对齐,跨端续播进度最坏 ~90s 内可见
    assert PULL_PERIOD_MS == 60_000


def test_push_versions_are_tracked_per_record() -> None:
    first = _record(key="first", updated_at=100)
    second = _record(key="second", updated_at=10)
    api = FakeApi()
    service = PlaybackHistorySyncService(api, FakeRepository([first, second]))

    service._pushed_versions[("site", "csp_Emby", "first")] = 200
    service._push()

    pushed_ids = [[payload["vodId"] for payload in batch] for batch in api.pushed]
    assert pushed_ids == [["second"]]


def test_push_skips_local_records_without_playback_progress() -> None:
    not_started = _record(key="not-started", updated_at=200, position=0)
    progressed = _record(key="progressed", updated_at=100, position=1)
    api = FakeApi()
    service = PlaybackHistorySyncService(api, FakeRepository([not_started, progressed]))

    service._push()

    assert [[payload["vodId"] for payload in batch] for batch in api.pushed] == [["progressed"]]
    assert ("site", "csp_Emby", "not-started") not in service._pushed_versions


def test_push_only_uploads_latest_100_records() -> None:
    records = [_record(key=f"vod-{index}", updated_at=index) for index in range(1, 102)]
    api = FakeApi()
    service = PlaybackHistorySyncService(api, FakeRepository(records))

    service._push()

    pushed_ids = {payload["vodId"] for payload in api.pushed[0]}
    assert len(pushed_ids) == 100
    assert "vod-101" in pushed_ids
    assert "vod-1" not in pushed_ids


def test_record_aging_out_of_latest_100_is_not_pushed_as_deletion() -> None:
    records = [_record(key=f"vod-{index}", updated_at=index) for index in range(1, 101)]
    repository = FakeRepository(records)
    api = FakeApi()
    service = PlaybackHistorySyncService(api, repository)
    service._push()

    repository.records[("emby", "", "vod-101")] = _record(key="vod-101", updated_at=101)
    service._push()

    assert [event["vodId"] for event in api.pushed[-1]] == ["vod-101"]
    assert all(event.get("event") != "playback.deleted" for event in api.pushed[-1])
    assert ("site", "csp_Emby", "vod-1") not in service._pushed_versions


def test_selection_context_round_trips_through_sync_payloads() -> None:
    record = _record(
        key="173",
        source_kind="spider_plugin",
        source_key="99",
        source_name="木偶",
        updated_at=100,
    )
    record.episode_url = "1@185535@6@1"
    record.playlist_index = 0
    record.source_group_index = 2
    record.source_index = 0
    record.source_subgroup_index = 6
    record.source_subgroup_name = "07外海风云"
    record.drive_dir_id = "local-only-dir"
    record.duration = 7_200_000
    stable_id = "02544b320a6d45de997bc0bd3975d0c060b8"
    api = FakeApi()
    service = PlaybackHistorySyncService(
        api,
        FakeRepository([record]),
        to_sync_source_key=lambda kind, key: stable_id if kind == "spider_plugin" else key,
    )

    service._push()

    payload = api.pushed[0][0]
    assert payload["vodId"] == "173"
    assert payload["episodeUrl"] == "1@185535@6@1"
    assert payload["durationMs"] == 7_200_000
    assert payload["playlistIndex"] == 0
    assert payload["sourceSubgroupIndex"] == 6
    assert payload["sourceSubgroupName"] == "07外海风云"

    repository = FakeRepository([])
    service = PlaybackHistorySyncService(
        FakeApi({"items": [payload], "nextSince": 1}),
        repository,
        to_local_source_key=lambda kind, key: "99" if kind == "spider_plugin" else key,
    )
    service._pull()

    assert repository.saved_payloads[0]["episodeUrl"] == "1@185535@6@1"
    assert repository.saved_payloads[0]["duration"] == 7_200_000
    assert repository.saved_payloads[0]["sourceSubgroupIndex"] == 6
    assert repository.saved_payloads[0]["sourceSubgroupName"] == "07外海风云"


def test_atv_source_alias_pushes_as_tvbox_site_identity() -> None:
    record = _record(key="v1", source_kind="telegram", updated_at=100)
    api = FakeApi()
    service = PlaybackHistorySyncService(api, FakeRepository([record]))

    service._push()

    payload = api.pushed[0][0]
    assert payload["sourceKind"] == "site"
    assert payload["sourceKey"] == "csp_TgDouBan"


def test_tvbox_site_aliases_round_trip_to_atv_sources() -> None:
    expected = {
        "csp_TgChannel": "telegram_channel",
        "csp_TgDouBan": "telegram",
        "csp_TgSearch": "telegram",
        "csp_TgWeb": "telegram",
        "csp_FishPanSou": "telegram",
        "csp_FishPanSouGroup": "telegram",
        "csp_AList": "browse",
        "csp_XiaoYa": "browse",
        "csp_BiliBili": "bilibili",
        "csp_FeiNiu": "feiniu",
        "csp_Jellyfin": "jellyfin",
    }

    for site_key, source_kind in expected.items():
        local_kind, local_key = PlaybackHistorySyncService._local_source("site", site_key)
        assert local_kind == source_kind
        assert PlaybackHistorySyncService._sync_source(local_kind, local_key) == (
            "site",
            site_key,
        )


def test_emby_pushes_and_pulls_with_tvbox_site_identity() -> None:
    record = _record(key="emby-1", source_kind="emby", updated_at=100)
    api = FakeApi()
    service = PlaybackHistorySyncService(api, FakeRepository([record]))

    service._push()

    assert api.pushed[0][0]["sourceKind"] == "site"
    assert api.pushed[0][0]["sourceKey"] == "csp_Emby"
    assert "csp_Emby" in service._current_pull_source_keys()


def test_tvbox_alist_site_pulls_as_atv_browse_source() -> None:
    repository = FakeRepository([])
    api = FakeApi(
        {
            "items": [
                {
                    "sourceKind": "site",
                    "sourceKey": "csp_AList",
                    "sourceName": "AList",
                    "vodId": "1$185535$1",
                    "updatedAt": 100,
                }
            ],
            "nextSince": 1,
        }
    )
    service = PlaybackHistorySyncService(api, repository)

    service._pull()

    assert repository.saved == [("browse", "csp_AList", "1$185535$1")]
    assert "site" in api.pull_source_kinds.split(",")
    assert "csp_AList" in api.pull_site_keys.split(",")


def test_tvbox_xiaoya_site_pulls_as_atv_browse_source() -> None:
    repository = FakeRepository([])
    api = FakeApi(
        {
            "items": [
                {
                    "sourceKind": "site",
                    "sourceKey": "csp_XiaoYa",
                    "sourceName": "小雅",
                    "vodId": "xiaoya-vod-1",
                    "updatedAt": 100,
                }
            ],
            "nextSince": 1,
        }
    )
    service = PlaybackHistorySyncService(api, repository)

    service._pull()

    assert repository.saved == [("browse", "csp_XiaoYa", "xiaoya-vod-1")]
    assert "csp_XiaoYa" in api.pull_site_keys.split(",")


def test_pull_preserves_local_opening_and_ending_markers() -> None:
    record = _record(key="v1", source_kind="emby", source_key="server", updated_at=100)
    record.opening = 12_000
    record.ending = 34_000
    repository = FakeRepository([record])
    api = FakeApi(
        {
            "items": [
                {
                    "sourceKind": "emby",
                    "sourceKey": "server",
                    "vodId": "v1",
                    "updatedAt": 200,
                }
            ]
        }
    )

    PlaybackHistorySyncService(api, repository)._pull()

    assert repository.saved_payloads[0]["opening"] == 12_000
    assert repository.saved_payloads[0]["ending"] == 34_000


def test_pull_applies_tombstones_before_advancing_cursor() -> None:
    repository = FakeRepository(
        [_record(key="removed", source_key="server", updated_at=10)]
    )
    api = FakeApi(
        {
            "deleted": [
                {
                    "sourceKind": "emby",
                    "sourceKey": "server",
                    "vodId": "removed",
                    "deletedAt": 100,
                }
            ],
            "items": [],
            "nextSince": 42,
        }
    )
    service = PlaybackHistorySyncService(api, repository)

    service._pull()

    assert repository.deleted == [("emby", "server", "removed")]
    assert service._pull_cursor == 42


def test_tombstone_without_deleted_at_is_skipped_not_mass_deleted() -> None:
    kept = _record(key="kept", source_key="server", updated_at=10)
    repository = FakeRepository([kept])
    api = FakeApi(
        {
            # scope=all 但缺失 deletedAt:曾导致无条件清空整库。现在必须保守跳过。
            "deleted": [{"scope": "all"}],
            "items": [],
            "nextSince": 42,
        }
    )
    service = PlaybackHistorySyncService(api, repository)

    service._pull()

    assert repository.get_history("emby", "kept", "server") is not None
    assert repository.deleted == []
    assert service._pull_cursor == 42


def test_stale_tombstone_keeps_newer_local_history() -> None:
    repository = FakeRepository(
        [_record(key="keep", source_key="server", updated_at=200)]
    )
    api = FakeApi(
        {
            "deleted": [
                {
                    "sourceKind": "emby",
                    "sourceKey": "server",
                    "vodId": "keep",
                    "deletedAt": 100,
                }
            ],
            "nextSince": 42,
        }
    )
    service = PlaybackHistorySyncService(api, repository)

    service._pull()

    assert repository.get_history("emby", "keep", "server") is not None
    assert repository.deleted == []


def test_scope_tombstones_delete_only_not_newer_rows() -> None:
    old = _record(key="old", source_key="server", updated_at=100)
    new = _record(key="new", source_key="server", updated_at=300)
    repository = FakeRepository([old, new])
    api = FakeApi(
        {
            "deleted": [
                {
                    "scope": "site",
                    "sourceKind": "emby",
                    "sourceKey": "server",
                    "deletedAt": 200,
                }
            ],
            "nextSince": 10,
        }
    )
    service = PlaybackHistorySyncService(api, repository)

    service._pull()

    assert repository.get_history("emby", "old", "server") is None
    assert repository.get_history("emby", "new", "server") is not None


def test_pull_ignores_sources_not_owned_by_local_history_repository() -> None:
    repository = FakeRepository([])
    api = FakeApi(
        {
            "items": [
                {
                    "sourceKind": "site",
                    "sourceKey": "tvbox-site",
                    "vodId": "remote-only",
                    "updatedAt": 100,
                }
            ],
            "nextSince": 10,
        }
    )
    service = PlaybackHistorySyncService(api, repository)

    service._pull()

    assert repository.saved == []
    assert service._pull_cursor == 10


def test_local_deletion_is_pushed_as_tombstone() -> None:
    record = _record(key="gone", source_key="server", updated_at=100)
    repository = FakeRepository([record])
    api = FakeApi()
    service = PlaybackHistorySyncService(api, repository)
    service._push()
    # 用户显式删除:本地删除 + 记入 pending 队列(由 HistoryController 触发)。
    repository.delete_history("emby", "gone", "server")
    repository.record_pending_deletion("emby", "server", "gone", 100)

    service._push()

    event = api.pushed[-1][0]
    assert event["event"] == "playback.deleted"
    assert event["vodId"] == "gone"
    assert event["deletedAt"] == 100


def test_record_vanishing_from_list_without_explicit_delete_is_not_tombstoned() -> None:
    # 回归保护:账户/命名空间切换会让记录从 list_histories() 消失,但只要不是显式删除,
    # 就绝不能上报 tombstone——否则会清空整库(2026-08-09 的 159→0 事故)。
    record = _record(key="gone", source_key="server", updated_at=100)
    repository = FakeRepository([record])
    api = FakeApi()
    service = PlaybackHistorySyncService(api, repository)
    service._push()

    # 模拟命名空间切换:记录仍在库里,但下一次扫描看不到它。
    repository.records.clear()

    service._push()

    # 没有显式删除就不应触发任何上报(尤其不能上报 tombstone)。
    assert len(api.pushed) == 1
    assert ("site", "csp_Emby", "gone") not in service._pushed_versions


def test_spider_plugin_push_uses_manifest_id_not_local_database_id() -> None:
    stable_id = "02544b320a6d45de997bc0bd3975d0c060b8"
    record = _record(
        key="https://115cdn.com/s/example",
        source_kind="spider_plugin",
        source_key="99",
        source_name="木偶（已重命名）",
        updated_at=100,
    )
    api = FakeApi()
    service = PlaybackHistorySyncService(
        api,
        FakeRepository([record]),
        to_sync_source_key=lambda kind, key: stable_id if kind == "spider_plugin" and key == "99" else key,
    )

    service._push()

    assert api.pushed[0][0]["sourceKey"] == stable_id
    assert api.pushed[0][0]["sourceName"] == "木偶（已重命名）"
    assert ("spider_plugin", stable_id, record.key) in service._pushed_versions


def test_spider_plugin_delete_keeps_stable_manifest_id_in_tombstone() -> None:
    stable_id = "02544b320a6d45de997bc0bd3975d0c060b8"
    record = _record(
        key="vod-1", source_kind="spider_plugin", source_key="99", updated_at=100
    )
    repository = FakeRepository([record])
    api = FakeApi()
    service = PlaybackHistorySyncService(
        api,
        repository,
        to_sync_source_key=lambda kind, key: stable_id if kind == "spider_plugin" else key,
    )
    service._push()
    repository.delete_history("spider_plugin", "vod-1", "99")
    repository.record_pending_deletion("spider_plugin", "99", "vod-1", 100)

    service._push()

    event = api.pushed[-1][0]
    assert event["event"] == "playback.deleted"
    assert event["sourceKey"] == stable_id


def test_spider_plugin_without_manifest_id_is_not_uploaded() -> None:
    record = _record(
        key="vod-1", source_kind="spider_plugin", source_key="99", updated_at=100
    )
    api = FakeApi()
    service = PlaybackHistorySyncService(api, FakeRepository([record]))

    service._push()

    assert api.pushed == []


def test_spider_plugin_pull_maps_manifest_id_to_local_database_id() -> None:
    stable_id = "02544b320a6d45de997bc0bd3975d0c060b8"
    repository = FakeRepository([])
    api = FakeApi(
        {
            "items": [
                {
                    "sourceKind": "spider_plugin",
                    "sourceKey": stable_id,
                    "sourceName": "木偶[盘]",
                    "vodId": "vod-1",
                    "updatedAt": 100,
                }
            ],
            "nextSince": 10,
        }
    )
    service = PlaybackHistorySyncService(
        api,
        repository,
        to_local_source_key=lambda kind, key: "99" if kind == "spider_plugin" and key == stable_id else key,
    )

    service._pull()

    assert repository.saved == [("spider_plugin", "99", "vod-1")]
    assert repository.saved_source_names == ["木偶[盘]"]
    assert ("spider_plugin", stable_id, "vod-1") in service._pushed_versions


def test_spider_plugin_pull_repairs_source_name_at_same_version() -> None:
    stable_id = "ff03a81ea2c940d4838e71fb21cf6651157d"
    existing = _record(
        key="vod-1",
        source_kind="spider_plugin",
        source_key="3",
        source_name="插件",
        updated_at=100,
    )
    repository = FakeRepository([existing])
    api = FakeApi(
        {
            "items": [
                {
                    "sourceKind": "spider_plugin",
                    "sourceKey": stable_id,
                    "sourceName": "短剧优选",
                    "vodId": "vod-1",
                    "updatedAt": 100,
                }
            ],
            "nextSince": 10,
        }
    )
    service = PlaybackHistorySyncService(
        api,
        repository,
        to_local_source_key=lambda kind, key: "3"
        if kind == "spider_plugin" and key == stable_id
        else key,
    )

    service._pull()

    assert repository.saved == [("spider_plugin", "3", "vod-1")]
    assert repository.saved_source_names == ["短剧优选"]


def test_installing_plugin_uses_fresh_filtered_pull_cursor() -> None:
    stable_id = "02544b320a6d45de997bc0bd3975d0c060b8"
    plugin_keys: list[str] = []
    repository = FakeRepository([])
    api = FakeApi({"nextSince": 10})
    service = PlaybackHistorySyncService(
        api,
        repository,
        playback_source_keys_loader=lambda: plugin_keys,
        to_local_source_key=lambda kind, key: "99" if kind == "spider_plugin" else key,
    )

    service._pull()
    plugin_keys.append(stable_id)
    api.page = {
        "items": [{
            "sourceKind": "spider_plugin",
            "sourceKey": stable_id,
            "vodId": "vod-1",
            "updatedAt": 100,
        }],
        "nextSince": 20,
    }
    service._pull()

    assert api.pull_since == [0, 0]
    assert stable_id in api.pull_site_keys.split(",")
    assert repository.saved == [("spider_plugin", "99", "vod-1")]


def test_pull_is_skipped_when_push_fails() -> None:
    pulled = False

    class FailingApi(FakeApi):
        def push_playback_events(self, records: list[dict]) -> None:
            del records
            raise RuntimeError("offline")

        def pull_playback_records(
            self, since: int, *, source_kinds: str = "", site_keys: str = ""
        ) -> dict:
            nonlocal pulled
            del since, source_kinds, site_keys
            pulled = True
            return {}

    service = PlaybackHistorySyncService(
        FailingApi(), FakeRepository([_record(key="one", updated_at=1)])
    )
    service._started = True

    service._run_sync()

    assert pulled is False


def test_sync_runs_outside_calling_thread() -> None:
    worker_called = threading.Event()

    class BlockingApi(FakeApi):
        def push_playback_events(self, records: list[dict]) -> None:
            assert threading.current_thread() is not threading.main_thread()
            worker_called.set()
            super().push_playback_events(records)

    service = PlaybackHistorySyncService(
        BlockingApi(),
        FakeRepository([_record(key="one", updated_at=1)]),
    )
    service._started = True
    service.sync()

    assert worker_called.wait(timeout=1)


def test_pull_soon_bypasses_pull_throttle() -> None:
    from time import monotonic

    pulled = []

    class PullApi(FakeApi):
        def pull_playback_records(
            self, since: int, *, source_kinds: str = "", site_keys: str = ""
        ) -> dict:
            pulled.append(since)
            return {}

    service = PlaybackHistorySyncService(PullApi(), FakeRepository([]))
    service._started = True

    # 距上次拉取 30s:仍在 PULL_PERIOD_MS(60s)周期内,但已过 pull_soon 节流(10s)
    service._last_pull_at = monotonic() - 30.0
    service.pull_soon()
    if service._worker is not None:
        service._worker.join(timeout=5.0)
    assert len(pulled) == 1  # pull_soon 越过周期节流,worker 立即补拉


def test_pull_soon_is_throttled_within_min_interval() -> None:
    from time import monotonic

    pulled = []

    class PullApi(FakeApi):
        def pull_playback_records(
            self, since: int, *, source_kinds: str = "", site_keys: str = ""
        ) -> dict:
            pulled.append(since)
            return {}

    service = PlaybackHistorySyncService(PullApi(), FakeRepository([]))
    service._started = True
    # 刚拉取过(_last_pull_at 距今 < PULL_SOON_MIN_INTERVAL_MS)→ 忽略本次请求
    service._last_pull_at = monotonic() - (PULL_SOON_MIN_INTERVAL_MS - 5_000) / 1000.0
    service.pull_soon()

    assert service._worker is None or not service._worker.is_alive()
    assert pulled == []

    # 超过最小间隔后允许再次触发
    service._last_pull_at = monotonic() - (PULL_SOON_MIN_INTERVAL_MS + 1_000) / 1000.0
    service.pull_soon()
    if service._worker is not None:
        service._worker.join(timeout=5.0)
    assert len(pulled) == 1


def test_pull_soon_is_noop_when_stopped() -> None:
    service = PlaybackHistorySyncService(FakeApi(), FakeRepository([]))
    service._started = False

    service.pull_soon()

    assert service._worker is None


def test_idle_push_is_capped_while_not_playing() -> None:
    # 暂停时 report_timer 仍每 5s 刷新本地记录 updated_at;未播放状态下
    # 最多 PUSH MAX_IDLE_PUSHES 次即暂停,不能无限上报相同进度。
    record = _record(key="paused", source_key="server", updated_at=100)
    repository = FakeRepository([record])
    api = FakeApi()
    service = PlaybackHistorySyncService(
        api, repository, is_playing_provider=lambda: False
    )
    service._started = True

    for _ in range(MAX_IDLE_PUSHES + 3):
        record.create_time += 1  # 模拟暂停期间周期性进度保存
        service._run_sync()

    assert len(api.pushed) == MAX_IDLE_PUSHES


def test_idle_push_quota_resets_when_playback_resumes() -> None:
    record = _record(key="paused", source_key="server", updated_at=100)
    repository = FakeRepository([record])
    api = FakeApi()
    playing = False
    service = PlaybackHistorySyncService(
        api, repository, is_playing_provider=lambda: playing
    )
    service._started = True

    for _ in range(MAX_IDLE_PUSHES + 1):
        record.create_time += 1
        service._run_sync()
    assert len(api.pushed) == MAX_IDLE_PUSHES

    playing = True  # 恢复播放:配额重置,进度继续上报
    record.create_time += 1
    service._run_sync()
    assert len(api.pushed) == MAX_IDLE_PUSHES + 1

    playing = False  # 再次暂停:重新限流
    for _ in range(MAX_IDLE_PUSHES + 2):
        record.create_time += 1
        service._run_sync()
    assert len(api.pushed) == 2 * MAX_IDLE_PUSHES + 1


def test_flush_pushes_final_progress_after_idle_cap() -> None:
    record = _record(key="paused", source_key="server", updated_at=100)
    repository = FakeRepository([record])
    api = FakeApi()
    service = PlaybackHistorySyncService(
        api, repository, is_playing_provider=lambda: False
    )
    service._started = True

    for _ in range(MAX_IDLE_PUSHES + 1):
        record.create_time += 1
        service._run_sync()
    assert len(api.pushed) == MAX_IDLE_PUSHES

    record.create_time += 1
    service.flush()  # 关闭前的最后一次 PUSH 不受未播放配额限制

    assert len(api.pushed) == MAX_IDLE_PUSHES + 1


def test_pull_continues_while_idle_push_is_capped() -> None:
    from time import monotonic

    record = _record(key="paused", source_key="server", updated_at=100)
    repository = FakeRepository([record])
    api = FakeApi()
    service = PlaybackHistorySyncService(
        api, repository, is_playing_provider=lambda: False
    )
    service._started = True
    for _ in range(MAX_IDLE_PUSHES + 1):
        record.create_time += 1
        service._run_sync()
    assert len(api.pushed) == MAX_IDLE_PUSHES

    # PUSH 已被限流,但 PULL 周期到了仍要执行(跨端进度仍需可见)
    service._last_pull_at = monotonic() - (PULL_PERIOD_MS / 1000.0 + 1.0)
    record.create_time += 1
    service._run_sync()

    assert len(api.pushed) == MAX_IDLE_PUSHES
    assert len(api.pull_since) >= 2


def test_push_without_playing_provider_stays_unlimited() -> None:
    # 未接线播放状态(如测试/旧调用方)时保持原有行为:有变更就上报。
    record = _record(key="idle", source_key="server", updated_at=100)
    repository = FakeRepository([record])
    api = FakeApi()
    service = PlaybackHistorySyncService(api, repository)
    service._started = True

    for _ in range(MAX_IDLE_PUSHES + 3):
        record.create_time += 1
        service._run_sync()

    assert len(api.pushed) == MAX_IDLE_PUSHES + 3


def test_msub_records_push_as_csp_media_and_round_trip() -> None:
    record = _record(key="msub:5", source_kind="msub", updated_at=100)
    record.episode_url = "msubep-5-3@0@2"
    api = FakeApi()
    service = PlaybackHistorySyncService(api, FakeRepository([record]))

    service._push()

    payload = api.pushed[0][0]
    # 与 web/TVBox 端共写同一 History 分区,服务端 watchedEpisode 按 vodId=msub:{id} 解析
    assert payload["sourceKind"] == "site"
    assert payload["sourceKey"] == "csp_Media"
    assert payload["vodId"] == "msub:5"
    assert "msubep-5-3" in payload["episodeUrl"]

    local_kind, local_key = PlaybackHistorySyncService._local_source("site", "csp_Media")
    assert (local_kind, local_key) == ("msub", "")
    assert PlaybackHistorySyncService._sync_source("msub", "http://192.168.50.60:4567") == ("site", "csp_Media")


def test_msub_pulled_records_land_in_local_history() -> None:
    payload = {
        "sourceKind": "site",
        "sourceKey": "csp_Media",
        "vodId": "msub:5",
        "episodeUrl": "msubep-5-3@0@2",
        "updatedAt": 300,
    }
    repository = FakeRepository([])
    service = PlaybackHistorySyncService(
        FakeApi({"items": [payload], "nextSince": 1}),
        repository,
    )

    service._pull()

    assert repository.saved[0] == ("msub", "", "msub:5")
    assert "msubep-5-3" in repository.saved_payloads[0]["episodeUrl"]
