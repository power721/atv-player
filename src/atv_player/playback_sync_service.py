# ruff: noqa: E501
from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from time import monotonic
from typing import Any

from PySide6.QtCore import QObject, QTimer

logger = logging.getLogger(__name__)

# 多端播放记录同步:周期性 PUSH 本地有播放进度的 Tier-B 记录 + PULL 服务端变更。
# 本服务覆盖本地播放历史；TvBox 站点 key 与 atv-player source_kind 在边界处互转。
# 鉴权复用 ApiClient 的 session 令牌(Authorization),服务端 resolveUid 解析为 uid。
INITIAL_DELAY_MS = 30_000
PERIOD_MS = 30_000
# PULL 是带游标的增量 GET,开销小;与安卓端(PlaybackSyncer 60s 周期)对齐,
# 让跨端续播进度在最坏 ~90s 内可见,而不是等满 5 分钟。
PULL_PERIOD_MS = 60_000
# pull_soon()(窗口重新激活等外部触发)的最小间隔,避免频繁切窗打爆服务端。
PULL_SOON_MIN_INTERVAL_MS = 10_000
# 未播放(暂停/无播放窗口)时最多 PUSH 3 次即暂停上报:暂停期间 report_timer
# 仍会周期性刷新本地记录的 updated_at,若不加限流,每个同步 tick 都会判定
# "有变更"而无限 PUSH 相同进度。恢复播放后配额重置;关闭前的 flush() 不受限。
MAX_IDLE_PUSHES = 3
SYNC_SOURCE_KINDS = frozenset(
    {
        "browse",
        "telegram",
        "telegram_channel",
        "bilibili",
        "youtube",
        "emby",
        "jellyfin",
        "feiniu",
        "direct_parse",
        "spider_plugin",
        "msub",
    }
)
SYNC_NAMESPACE_VERSION = "v7-plugin-source-name"
SYNC_LIMIT = 100
TVBOX_SITE_TO_ATV_KIND = {
    "csp_TgDouBan": "telegram",
    "csp_TgChannel": "telegram_channel",
    "csp_TgSearch": "telegram",
    "csp_TgWeb": "telegram",
    "csp_FishPanSou": "telegram",
    "csp_FishPanSouGroup": "telegram",
    "csp_AList": "browse",
    "csp_XiaoYa": "browse",
    "csp_BiliBili": "bilibili",
    "csp_FeiNiu": "feiniu",
    "csp_Emby": "emby",
    "csp_Jellyfin": "jellyfin",
    # 服务端追剧:与 web/TVBox 端共写同一 History 分区,观看进度互通
    "csp_Media": "msub",
}
ATV_KIND_TO_TVBOX_SITE = {
    "telegram": "csp_TgDouBan",
    "telegram_channel": "csp_TgChannel",
    "bilibili": "csp_BiliBili",
    "feiniu": "csp_FeiNiu",
    "emby": "csp_Emby",
    "jellyfin": "csp_Jellyfin",
    "msub": "csp_Media",
}
TELEGRAM_SITE_KEYS = frozenset(
    {"csp_TgDouBan", "csp_TgSearch", "csp_TgWeb", "csp_FishPanSou", "csp_FishPanSouGroup"}
)
BROWSE_SITE_KEYS = frozenset({"csp_AList", "csp_XiaoYa"})
SYNC_PULL_SOURCE_KINDS = tuple(sorted(SYNC_SOURCE_KINDS | {"site"}))
SYNC_PULL_SITE_KEYS = tuple(sorted(TVBOX_SITE_TO_ATV_KIND))
SourceKeyResolver = Callable[[str, str], str | None]
SourceKeysLoader = Callable[[], list[str]]
PlayingStateProvider = Callable[[], bool]


class PlaybackHistorySyncService(QObject):
    def __init__(
        self,
        api_client,
        repository,
        *,
        installation_id: str = "",
        to_sync_source_key: SourceKeyResolver | None = None,
        to_local_source_key: SourceKeyResolver | None = None,
        playback_source_keys_loader: SourceKeysLoader | None = None,
        is_playing_provider: PlayingStateProvider | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._api = api_client
        self._repo = repository
        self._client_key = installation_id
        identity = str(getattr(api_client, "playback_sync_identity", "") or "default")
        self._namespace = f"{identity}:{SYNC_NAMESPACE_VERSION}"
        self._sync_key_resolver = to_sync_source_key or self._default_source_key_resolver
        self._local_key_resolver = to_local_source_key or self._default_source_key_resolver
        self._playback_source_keys_loader = playback_source_keys_loader or (lambda: [])
        self._is_playing_provider = is_playing_provider
        self._idle_pushes = 0
        self._idle_push_logged = False
        self._unmapped_plugins: set[tuple[str, str]] = set()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.sync)
        load_snapshot = getattr(self._repo, "load_sync_snapshot", lambda _namespace: {})
        load_cursor = getattr(self._repo, "get_sync_cursor", lambda _namespace: 0)
        self._pushed_versions: dict[tuple[str, str, str], int] = load_snapshot(self._namespace)
        self._pull_source_keys = self._current_pull_source_keys()
        self._cursor_namespace = self._build_cursor_namespace(self._pull_source_keys)
        self._pull_cursor = load_cursor(self._cursor_namespace)
        self._sync_lock = threading.Lock()
        self._sync_in_progress = False
        self._started = False
        self._worker = None
        self._last_pull_at = 0.0

    def start(self) -> None:
        self._started = True
        QTimer.singleShot(INITIAL_DELAY_MS, self.sync)
        self._timer.start(PERIOD_MS)
        logger.info(
            "playback sync started: initial_delay_ms=%d push_period_ms=%d pull_period_ms=%d cursor=%s snapshot=%d",
            INITIAL_DELAY_MS,
            PERIOD_MS,
            PULL_PERIOD_MS,
            self._pull_cursor,
            len(self._pushed_versions),
        )

    def stop(self) -> None:
        self._started = False
        self._timer.stop()

    def pull_soon(self) -> None:
        """请求尽快 PULL 一次(如窗口重新激活),越过 PULL_PERIOD_MS 节流。

        仍受 PULL_SOON_MIN_INTERVAL_MS 限流;服务未启动时是空操作。
        """
        if not self._started:
            return
        now = monotonic()
        if self._last_pull_at > 0 and (now - self._last_pull_at) * 1000 < PULL_SOON_MIN_INTERVAL_MS:
            return
        self._last_pull_at = 0.0
        self.sync()

    def flush(self) -> None:
        """关闭/登出前同步执行最后一次 PUSH,把未到 tick 的进度上报到服务端。

        Qt 进程退出会强杀守护线程,故最终 PUSH 必须在调用线程内联执行而非再交给 worker;
        先 join 正在运行的 worker 以避免并发双推。PULL 留待下次启动续上。
        """
        self._started = False
        self._timer.stop()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=10.0)
        try:
            self._push()
        except Exception as exc:  # noqa: BLE001 - 关闭路径不能抛
            logger.warning("playback sync final flush failed: %s", exc)

    def sync(self) -> None:
        """Schedule one sync without blocking the Qt event loop.

        The repository opens a SQLite connection per operation, and ApiClient's
        HTTP client is safe to use from this worker thread.  Keeping the whole
        sync sequence in one worker also keeps its cursors and version snapshots
        single-threaded.
        """
        if not self._started:
            return
        with self._sync_lock:
            if self._sync_in_progress:
                return
            self._sync_in_progress = True
        self._worker = threading.Thread(
            target=self._run_sync,
            name="playback-history-sync",
            daemon=True,
        )
        self._worker.start()

    def _run_sync(self) -> None:
        try:
            playing = self._playing()
            if playing:
                self._idle_pushes = 0
                self._idle_push_logged = False
            push_succeeded = True
            if playing or self._idle_pushes < MAX_IDLE_PUSHES:
                try:
                    pushed = self._push()
                except Exception as exc:  # noqa: BLE001 - 后台同步不能让异常冒泡到 Qt
                    push_succeeded = False
                    logger.warning("playback sync push failed: %s", exc)
                else:
                    # 只计实际发出上报的轮次;空扫描不消耗配额,失败留给下个 tick 重试。
                    if pushed and not playing:
                        self._idle_pushes += 1
                        if self._idle_pushes >= MAX_IDLE_PUSHES and not self._idle_push_logged:
                            self._idle_push_logged = True
                            logger.info(
                                "playback sync push paused after %d idle pushes; will resume on playback",
                                self._idle_pushes,
                            )
            pull_due = self._last_pull_at <= 0 or (
                monotonic() - self._last_pull_at
            ) * 1000 >= PULL_PERIOD_MS
            if self._started and push_succeeded and pull_due:
                try:
                    self._pull()
                    self._last_pull_at = monotonic()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("playback sync pull failed: %s", exc)
        finally:
            with self._sync_lock:
                self._sync_in_progress = False

    def _playing(self) -> bool:
        """探测是否有播放器正在播放;未接线或探测失败时按"播放中"处理,不限制同步。"""
        provider = self._is_playing_provider
        if provider is None:
            return True
        try:
            return bool(provider())
        except Exception:  # noqa: BLE001 - 状态探测不能中断同步
            return True

    # ── PUSH:本地 Tier-B → 服务端 ──────────────────────────────────────────

    def _push(self) -> bool:
        """扫描本地记录并上报变更。返回是否实际发出了上报(空扫描返回 False)。"""
        all_records = sorted(
            [
                record
                for record in self._repo.list_histories()
                # 仅本地创建、但尚未实际播放的记录(position=0)不应写入服务端。
                # 保留在本地后，首次产生进度时会因 updated_at 变化正常被 PUSH。
                if record.source_kind in SYNC_SOURCE_KINDS and int(record.position or 0) > 0
            ],
            key=lambda record: int(record.create_time or 0),
            reverse=True,
        )
        records = all_records[:SYNC_LIMIT]
        current_versions: dict[tuple[str, str, str], int] = {}
        changed: list[tuple[tuple[str, str, str], int, dict[str, Any]]] = []
        for record in records:
            record_key = self._resolved_sync_identity(
                record.source_kind, record.source_key, record.key, self._sync_key_resolver
            )
            if record_key is None:
                self._warn_unmapped_plugin("push", record.source_key)
                continue
            payload = self._to_payload(record, record_key[0], record_key[1])
            updated_at = int(payload.get("updatedAt", 0) or 0)
            # 同一同步身份可能对应多行本地记录(source_key 别名迁移遗留的 ''/csp_* 双行,
            # 且别名解析后归并为同一 site 身份)。扫描按 create_time 新→旧,后来的行只会
            # 更旧:快照版本必须只升不降,否则旧行把快照钉回旧版本,新行每个 tick 都被判
            # "有变更"而无限重推同一进度(2026-08-31 服务端每 30s 重复 skip not newer)。
            if updated_at <= current_versions.get(record_key, -1):
                continue
            current_versions[record_key] = updated_at
            if updated_at > self._pushed_versions.get(record_key, -1):
                changed.append((record_key, updated_at, payload))
        # 删除只走显式通道:用户删除时写入 pending 队列,这里转成 tombstone 上报。
        # 不再用 list 差集推断删除——那会在账户/命名空间切换时把"不可见"的全量记录
        # 误判为已删除并上报,配合服务端墓碑回灌清空整库(2026-08-09 实测 159→0)。
        tombstones, consumed_deletes = self._build_pending_tombstones()
        logger.info(
            "playback sync scan: local=%d latest=%d snapshot=%d updates=%d deletes=%d",
            len(all_records),
            len(records),
            len(self._pushed_versions),
            len(changed),
            len(tombstones),
        )
        if not changed and not tombstones:
            if current_versions != self._pushed_versions:
                self._repo.replace_sync_snapshot(self._namespace, current_versions)
                self._pushed_versions = current_versions
            return False
        self._api.push_playback_events([payload for _, _, payload in changed] + tombstones)
        logger.info(
            "playback sync push succeeded: updates=%d deletes=%d",
            len(changed),
            len(tombstones),
        )
        # 仅在上报成功后清掉已消费的 pending 删除;失败则留待下个 tick 重试。
        if consumed_deletes:
            self._repo.clear_pending_deletions(consumed_deletes)
        self._repo.replace_sync_snapshot(self._namespace, current_versions)
        self._pushed_versions = current_versions
        return True

    def _build_pending_tombstones(self) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
        """把本地 pending 删除队列转成服务端 tombstone。

        返回 ``(tombstones, consumed)``:``tombstones`` 用于上报;``consumed`` 是上报成功后
        需要从 pending 表清掉的原始 (source_kind, source_key, vod_id)。无法解析同步身份的
        记录(如缺稳定 manifest id 的 spider_plugin)无法上报,直接丢弃并告警。
        """
        tombstones: list[dict[str, Any]] = []
        consumed: list[tuple[str, str, str]] = []
        undeliverable: list[tuple[str, str, str]] = []
        for source_kind, source_key, vod_id, deleted_at in self._repo.list_pending_deletions():
            sync_identity = self._resolved_sync_identity(
                source_kind, source_key, vod_id, self._sync_key_resolver
            )
            if sync_identity is None:
                self._warn_unmapped_plugin("delete", source_key)
                undeliverable.append((source_kind, source_key, vod_id))
                continue
            tombstones.append(
                {
                    "event": "playback.deleted",
                    "scope": "item",
                    "sourceKind": sync_identity[0],
                    "sourceKey": sync_identity[1],
                    "vodId": sync_identity[2],
                    "deletedAt": max(1, int(deleted_at or 1)),
                }
            )
            consumed.append((source_kind, source_key, vod_id))
        if undeliverable:
            self._repo.clear_pending_deletions(undeliverable)
        return tombstones, consumed

    @staticmethod
    def _record_key(source_kind: str, source_key: str, vod_id: str) -> tuple[str, str, str]:
        return source_kind, source_key, vod_id

    def _to_payload(self, record, sync_source_kind: str, sync_source_key: str) -> dict[str, Any]:
        return {
            "sourceKind": sync_source_kind,
            "sourceKey": sync_source_key,
            "sourceName": record.source_name,
            "vodId": record.key,
            "vodName": record.vod_name,
            "vodPic": record.vod_pic,
            "episodeName": record.vod_remarks,
            "episode": record.episode,
            "episodeUrl": record.episode_url,
            "positionMs": record.position,
            "durationMs": record.duration,
            "openingMs": record.opening,
            "endingMs": record.ending,
            "updatedAt": record.create_time,
            "speed": record.speed,
            "clientKey": self._client_key,
            "playlistIndex": record.playlist_index,
            "sourceGroupIndex": record.source_group_index,
            "sourceIndex": record.source_index,
            "sourceSubgroupIndex": record.source_subgroup_index,
            "sourceSubgroupName": record.source_subgroup_name,
            "driveDirId": record.drive_dir_id,
            "driveShareKey": record.drive_share_key,
            "drivePath": record.drive_path,
        }

    # ── PULL:服务端 → 本地 Tier-B(LWW by updated_at) ──────────────────────

    def _pull(self) -> None:
        pull_source_keys = self._current_pull_source_keys()
        cursor_namespace = self._build_cursor_namespace(pull_source_keys)
        if cursor_namespace != self._cursor_namespace:
            self._pull_source_keys = pull_source_keys
            self._cursor_namespace = cursor_namespace
            self._pull_cursor = self._repo.get_sync_cursor(cursor_namespace)
        page = self._api.pull_playback_records(
            self._pull_cursor,
            source_kinds=",".join(SYNC_PULL_SOURCE_KINDS),
            site_keys=",".join(pull_source_keys),
        )
        deleted = page.get("deleted") or []
        items = page.get("items") or []
        logger.info(
            "playback sync pull: since=%s items=%d deleted=%d next=%s",
            self._pull_cursor,
            len(items),
            len(deleted),
            page.get("nextSince"),
        )

        # Process tombstones first: an item in the same page may be a newer
        # re-created record and should therefore be allowed to save afterwards.
        for tombstone in deleted:
            if not isinstance(tombstone, dict):
                continue
            scope = str(tombstone.get("scope") or "item").strip().lower()
            deleted_at = int(
                tombstone.get("deletedAt")
                or tombstone.get("deleted_at")
                or tombstone.get("timestamp")
                or 0
            )
            if deleted_at <= 0:
                # 服务端协议保证 deletedAt 非空(NOT NULL);缺失时保守跳过,
                # 绝不退化为无条件全量删除(scope=all 会清空整库)。
                logger.warning("playback sync: skip tombstone without deletedAt scope=%s", scope)
                continue
            removed: list[tuple[str, str, str]] = []
            if scope == "all":
                removed = self._repo.delete_all_histories(deleted_at)
                removed.extend(
                    identity
                    for identity, updated_at in self._pushed_versions.items()
                    if deleted_at <= 0 or updated_at <= deleted_at
                )
            elif scope == "site":
                sync_source_kind = str(tombstone.get("sourceKind") or tombstone.get("source_kind") or "site")
                sync_source_key = str(tombstone.get("sourceKey") or tombstone.get("source_key") or "")
                source_kind, raw_local_source_key = self._local_source(sync_source_kind, sync_source_key)
                local_source_key = self._resolve_source_key(
                    source_kind, raw_local_source_key, self._local_key_resolver
                )
                if source_kind in SYNC_SOURCE_KINDS and local_source_key is not None:
                    removed = self._repo.delete_site_history(source_kind, local_source_key, deleted_at)
                    removed = [
                        self._record_key(sync_source_kind, sync_source_key, identity[2])
                        for identity in removed
                    ]
                    removed.extend(
                        identity
                        for identity, updated_at in self._pushed_versions.items()
                        if identity[0] == sync_source_kind
                        and identity[1] == sync_source_key
                        and (deleted_at <= 0 or updated_at <= deleted_at)
                    )
            else:
                sync_identity = self._item_identity(tombstone)
                if sync_identity is None:
                    continue
                local_identity = self._resolved_local_identity(*sync_identity, self._local_key_resolver)
                if local_identity is None or local_identity[0] not in SYNC_SOURCE_KINDS:
                    self._warn_unmapped_plugin("pull delete", sync_identity[1])
                    continue
                source_kind, source_key, vod_id = local_identity
                existing = self._repo.get_history(source_kind, vod_id, source_key)
                if existing is not None and deleted_at > 0 and existing.create_time > deleted_at:
                    continue
                self._repo.delete_history(source_kind, vod_id, source_key)
                removed = [sync_identity]
            for identity in set(removed):
                self._repo.remove_sync_snapshot(self._namespace, identity)
                self._pushed_versions.pop(identity, None)

        for item in items:
            sync_identity = self._item_identity(item)
            if sync_identity is None:
                continue
            local_identity = self._resolved_local_identity(*sync_identity, self._local_key_resolver)
            if local_identity is None or local_identity[0] not in SYNC_SOURCE_KINDS:
                self._warn_unmapped_plugin("pull", sync_identity[1])
                continue
            source_kind, source_key, vod_id = local_identity
            updated_at = int(item.get("updatedAt") or item.get("updated_at") or item.get("timestamp") or 0)
            source_name = str(item.get("sourceName") or item.get("source_name") or "")
            existing = self._repo.get_history(source_kind, vod_id, source_key)
            if existing is not None:
                if existing.create_time > updated_at:
                    continue  # 本地进度更新,跳过远端旧版本
                if existing.create_time == updated_at and (
                    not source_name or existing.source_name == source_name
                ):
                    continue  # 同版本且来源名无需修复
            payload = {
                "vodName": item.get("vodName") or item.get("vod_name") or "",
                "vodPic": item.get("vodPic") or item.get("vod_pic") or "",
                "vodRemarks": item.get("episodeName") or item.get("vodRemarks") or "",
                "episode": int(item.get("episode") or 0),
                "episodeUrl": item.get("episodeUrl") or item.get("episode_url") or "",
                "position": int(item.get("positionMs") or item.get("position") or 0),
                "duration": int(item.get("durationMs") or item.get("duration") or 0),
                # 服务端协议不包含片头片尾；远端进度更新不能清空本地标记。
                "opening": existing.opening if existing is not None else 0,
                "ending": existing.ending if existing is not None else 0,
                "speed": float(item.get("speed") or 1.0),
                "createTime": updated_at,
                "playlistIndex": int(item.get("playlistIndex") or item.get("playlist_index") or 0),
                "sourceGroupIndex": int(item.get("sourceGroupIndex") or item.get("source_group_index") or 0),
                "sourceIndex": int(item.get("sourceIndex") or item.get("source_index") or 0),
                "sourceSubgroupIndex": int(
                    item.get("sourceSubgroupIndex") or item.get("source_subgroup_index") or 0
                ),
                "sourceSubgroupName": item.get("sourceSubgroupName")
                or item.get("source_subgroup_name")
                or "",
                "driveDirId": item.get("driveDirId") or item.get("drive_dir_id") or "",
                "driveShareKey": item.get("driveShareKey")
                or item.get("drive_share_key")
                or "",
                "drivePath": item.get("drivePath") or item.get("drive_path") or "",
            }
            self._repo.save_history(source_kind, vod_id, payload, source_key=source_key, source_name=source_name)
            self._repo.set_sync_snapshot_version(self._namespace, sync_identity, updated_at)
            self._pushed_versions[sync_identity] = updated_at
        next_since = page.get("nextSince")
        if next_since is not None:
            try:
                self._pull_cursor = int(next_since)
                self._repo.set_sync_cursor(self._cursor_namespace, self._pull_cursor)
            except (TypeError, ValueError):
                pass

    def _current_pull_source_keys(self) -> tuple[str, ...]:
        plugin_keys = {
            str(value).strip()
            for value in self._playback_source_keys_loader()
            if str(value).strip()
        }
        return tuple(sorted(set(SYNC_PULL_SITE_KEYS) | plugin_keys))

    def _build_cursor_namespace(self, source_keys: tuple[str, ...]) -> str:
        digest = hashlib.sha256("\n".join(source_keys).encode()).hexdigest()[:16]
        return f"{self._namespace}:pull:{digest}"

    @staticmethod
    def _item_identity(item: Any) -> tuple[str, str, str] | None:
        if not isinstance(item, dict):
            return None
        source_kind = str(item.get("sourceKind") or item.get("source_kind") or "")
        source_key = str(item.get("sourceKey") or item.get("source_key") or "")
        vod_id = str(item.get("vodId") or item.get("vod_id") or "")
        if not vod_id:
            return None
        return PlaybackHistorySyncService._record_key(source_kind, source_key, vod_id)

    @staticmethod
    def _default_source_key_resolver(source_kind: str, source_key: str) -> str | None:
        if source_kind == "spider_plugin":
            return None
        return source_key

    @staticmethod
    def _resolve_source_key(
        source_kind: str, source_key: str, resolver: SourceKeyResolver
    ) -> str | None:
        resolved = resolver(source_kind, source_key)
        if resolved is None:
            return None
        value = str(resolved)
        if source_kind == "spider_plugin" and not value:
            return None
        return value

    @staticmethod
    def _sync_source(source_kind: str, source_key: str) -> tuple[str, str]:
        if source_kind == "browse":
            return "site", source_key if source_key in BROWSE_SITE_KEYS else "csp_AList"
        if source_kind == "telegram":
            return "site", source_key if source_key in TELEGRAM_SITE_KEYS else "csp_TgDouBan"
        site_key = ATV_KIND_TO_TVBOX_SITE.get(source_kind)
        return ("site", site_key) if site_key else (source_kind, source_key)

    @staticmethod
    def _local_source(source_kind: str, source_key: str) -> tuple[str, str]:
        if source_kind != "site":
            return source_kind, source_key
        local_kind = TVBOX_SITE_TO_ATV_KIND.get(source_key)
        if local_kind is None:
            return source_kind, source_key
        return local_kind, source_key if local_kind in {"browse", "telegram"} else ""

    @classmethod
    def _resolved_sync_identity(
        cls,
        source_kind: str,
        source_key: str,
        vod_id: str,
        resolver: SourceKeyResolver,
    ) -> tuple[str, str, str] | None:
        sync_source_kind, raw_sync_source_key = cls._sync_source(source_kind, source_key)
        resolved = cls._resolve_source_key(sync_source_kind, raw_sync_source_key, resolver)
        if resolved is None:
            return None
        return cls._record_key(sync_source_kind, resolved, vod_id)

    @classmethod
    def _resolved_local_identity(
        cls,
        source_kind: str,
        source_key: str,
        vod_id: str,
        resolver: SourceKeyResolver,
    ) -> tuple[str, str, str] | None:
        local_source_kind, raw_local_source_key = cls._local_source(source_kind, source_key)
        resolved = cls._resolve_source_key(local_source_kind, raw_local_source_key, resolver)
        if resolved is None:
            return None
        return cls._record_key(local_source_kind, resolved, vod_id)

    def _warn_unmapped_plugin(self, direction: str, source_key: str) -> None:
        marker = direction, source_key
        if marker in self._unmapped_plugins:
            return
        self._unmapped_plugins.add(marker)
        logger.warning(
            "playback sync skips spider plugin without stable manifest id: direction=%s source_key=%s",
            direction,
            source_key,
        )
