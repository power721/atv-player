# ruff: noqa: E501
"""服务端追剧系统(alist-tvbox media-subscription)信号同步。

把服务端订阅的"挂载资源实际可播集数"(currentEpisodes)并入本地追更记录:
latest_episode 取 max、写入 msub 播放绑定、按需抬首页追更提示。
设计约束见 apply_backend_signal——本服务必须在元数据巡检落库之后运行。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger(__name__)

INITIAL_DELAY_MS = 90_000
SYNC_INTERVAL_MS = 60 * 60 * 1000
# 窗口激活等外部触发的最小间隔,避免频繁切窗打爆服务端(对齐播放同步 pull_soon 口径)。
SYNC_SOON_MIN_INTERVAL_SECONDS = 300
# 匹配失败(含自动订阅失败)的负缓存:避免每轮同步都重复 POST——
# 后端 create 幂等但会再次触发首轮巡检(五路搜索+挂载,数分钟/条)。
NEGATIVE_CACHE_TTL_SECONDS = 7 * 24 * 3600
# 自动订阅逐条创建之间隔开,给服务端巡检让路。
AUTO_SUBSCRIBE_STAGGER_SECONDS = 30

_MOVIE_MEDIA_KINDS = {"movie", "电影"}

_SPACE_RE = re.compile(r"[\s\u3000]+")


def _normalize_title(value: object) -> str:
    text = str(value or "").strip().lower()
    return _SPACE_RE.sub("", text)


def _tmdb_series_id(value: object) -> str:
    """external_ids 里的 tmdb 标识可能是 "tv:123" 或裸 id,统一取剧集数字 id。"""
    text = str(value or "").strip()
    if text.startswith("tv:"):
        text = text.split(":", 2)[1]
    digits = re.sub(r"\D", "", text)
    return digits


def _as_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class _SubscriptionIndex:
    """按 tmdb id / 豆瓣 id / 归一化标题+季 三级匹配服务端订阅。"""

    def __init__(self, subscriptions: list[dict[str, Any]]) -> None:
        self._by_tmdb: dict[tuple[str, int], dict[str, Any]] = {}
        self._by_douban: dict[tuple[int, int], dict[str, Any]] = {}
        self._by_title: dict[tuple[str, int], dict[str, Any]] = {}
        for sub in subscriptions:
            self._put(self._by_tmdb, (_tmdb_series_id(sub.get("metaId")) if str(sub.get("metaProvider") or "") == "tmdb" else "", _as_int(sub.get("season")) or 1), sub)
            self._put(self._by_douban, (_as_int(sub.get("doubanId")), _as_int(sub.get("season")) or 1), sub)
            self._put(self._by_title, (_normalize_title(sub.get("name")), _as_int(sub.get("season")) or 1), sub)

    def _put(self, index: dict[tuple, dict[str, Any]], key: tuple, sub: dict[str, Any]) -> None:
        if not key[0]:
            return
        existing = index.get(key)
        # 同键多条(历史/别名)时取可播集数多的一条,信号更足。
        if existing is None or _as_int(sub.get("currentEpisodes")) > _as_int(existing.get("currentEpisodes")):
            index[key] = sub

    def match(self, record) -> dict[str, Any] | None:
        indexes = {"tmdb": self._by_tmdb, "douban": self._by_douban, "title": self._by_title}
        for kind, value, season in _record_match_keys(record):
            sub = indexes[kind].get((value, season))
            if sub is not None:
                return sub
        return None


def _record_match_keys(record) -> list[tuple[str, object, int]]:
    """本地记录的可匹配键(tmdb → 豆瓣 → 归一化标题,均带季),正反向匹配共用。

    豆瓣键保持 int、tmdb/标题为 str,与 _SubscriptionIndex 各索引的键型一致。
    """
    keys: list[tuple[str, object, int]] = []
    season = int(getattr(record, "season_number", 0) or 0) or 1
    external_ids = {str(k): str(v) for k, v in dict(getattr(record, "external_ids", {}) or {}).items() if str(v or "").strip()}
    tmdb_id = _tmdb_series_id(external_ids.get("tmdb"))
    if tmdb_id:
        keys.append(("tmdb", tmdb_id, season))
    douban_id = _as_int(external_ids.get("douban"))
    if douban_id:
        keys.append(("douban", douban_id, season))
    title = _normalize_title(getattr(record, "title", ""))
    if title:
        keys.append(("title", title, season))
    return keys


def find_missing_subscriptions(subscriptions: list[dict[str, Any]], records) -> list[dict[str, Any]]:
    """筛选没有任何本地追更记录对应的服务端订阅(导入候选)。

    匹配键与 _SubscriptionIndex 相同(tmdb id → 豆瓣 id → 归一化标题,均带季),
    保证"同步时能匹配上的订阅导入时必然被判为已存在",两条路径不会各说各话。
    """
    record_keys: set[tuple[str, object, int]] = set()
    for record in records:
        record_keys.update(_record_match_keys(record))
    missing: list[dict[str, Any]] = []
    for sub in subscriptions:
        season = _as_int(sub.get("season")) or 1
        matched = False
        tmdb_id = _tmdb_series_id(sub.get("metaId")) if str(sub.get("metaProvider") or "") == "tmdb" else ""
        if tmdb_id and ("tmdb", tmdb_id, season) in record_keys:
            matched = True
        douban_id = _as_int(sub.get("doubanId"))
        if not matched and douban_id and ("douban", douban_id, season) in record_keys:
            matched = True
        if not matched and ("title", _normalize_title(sub.get("name")), season) in record_keys:
            matched = True
        if not matched:
            missing.append(sub)
    return missing


@dataclass(slots=True)
class BackendSubscriptionImportResult:
    """从服务端订阅导入本地追更的结果汇总。"""

    total: int = 0
    imported: int = 0
    skipped: int = 0
    cancelled: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)
    skips: list[tuple[str, str]] = field(default_factory=list)

    @property
    def summary_text(self) -> str:
        parts = [f"服务端待导入 {self.total} 条", f"新增 {self.imported} 条"]
        if self.skipped:
            parts.append(f"跳过 {self.skipped} 条")
        if self.failures:
            parts.append(f"失败 {len(self.failures)} 条")
        text = "，".join(parts)
        if self.cancelled:
            text = "已取消 · " + text
        for name, reason in [*self.skips, *self.failures][:5]:
            text += f"\n· {name or '未命名订阅'}: {reason}"
        extra = len(self.skips) + len(self.failures) - 5
        if extra > 0:
            text += f"\n· …其余 {extra} 条见日志"
        return text


class FollowingBackendSyncService(QObject):
    # 每次同步产生变化的 (record_id, changed) 列表;仅在至少一条变化时发射。
    sync_finished = Signal(object)

    def __init__(
        self,
        api_client,
        repository,
        *,
        settings_provider: Callable[[], dict[str, bool]],
        now: Callable[[], int] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._api = api_client
        self._repository = repository
        self._settings_provider = settings_provider
        self._now = now or (lambda: int(time.time()))
        self._negative_cache: dict[tuple[str, str], int] = {}
        self._sync_lock = threading.Lock()
        self._last_sync_soon_at = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_timer)

    # ---- 调度 ----

    def start(self) -> None:
        QTimer.singleShot(INITIAL_DELAY_MS, self._on_timer)
        self._timer.start(SYNC_INTERVAL_MS)

    def stop(self) -> None:
        self._timer.stop()

    def sync_soon(self) -> None:
        """外部触发(窗口激活/开关后回到前台),限频防切窗风暴;定时轮次不受限。"""
        settings = self._settings()
        if not settings.get("enabled"):
            return
        now = time.monotonic()
        if now - self._last_sync_soon_at < SYNC_SOON_MIN_INTERVAL_SECONDS:
            return
        self._last_sync_soon_at = now
        self._on_timer()

    def _on_timer(self) -> None:
        settings = self._settings()
        if not settings.get("enabled"):
            return
        if not self._sync_lock.acquire(blocking=False):
            return
        threading.Thread(target=self._sync_async, daemon=True).start()

    def _sync_async(self) -> None:
        try:
            self._run_sync(emit=True)
        except Exception:
            logger.warning("following backend sync failed", exc_info=True)
        finally:
            self._sync_lock.release()

    # ---- 同步主体 ----

    def sync_blocking(self) -> list[tuple[int, bool]]:
        """同步执行一轮(手动检查更新路径,UI 线程调用);失败/占用静默返回空列表。

        后台同步进行中时不等待(锁会被 worker 持有数分钟),本轮跳过——
        后台那轮自己会落库,结果经 sync_finished 通知 UI。
        """
        if not self._settings().get("enabled"):
            return []
        if not self._sync_lock.acquire(blocking=False):
            return []
        try:
            return self._run_sync(emit=False, stagger=False)
        except Exception:
            logger.warning("following backend sync failed", exc_info=True)
            return []
        finally:
            self._sync_lock.release()

    def _run_sync(self, *, emit: bool, stagger: bool = True) -> list[tuple[int, bool]]:
        subscriptions = list(self._api.list_media_subscriptions())
        index = _SubscriptionIndex(subscriptions)
        records, _total = self._repository.load_page(page=1, size=1000, keyword="", only_updates=False)
        settings = self._settings()
        auto_subscribe = bool(settings.get("auto_subscribe"))
        now = self._now()
        results: list[tuple[int, bool]] = []
        pending_auto_subscribe: list = []
        for record in records:
            if str(getattr(record, "media_kind", "") or "").strip().lower() in _MOVIE_MEDIA_KINDS:
                continue
            sub = index.match(record)
            if sub is not None:
                if self._apply(record, sub, now):
                    results.append((record.id, True))
                continue
            if auto_subscribe:
                pending_auto_subscribe.append(record)
        for position, record in enumerate(pending_auto_subscribe):
            # 错峰只用于后台定时路径;手动路径(UI 线程同步等待)不再 sleeps。
            if stagger and position > 0:
                time.sleep(AUTO_SUBSCRIBE_STAGGER_SECONDS)
            sub = self._auto_subscribe(record)
            if sub is not None and self._apply(record, sub, now):
                results.append((record.id, True))
        if results and emit:
            self.sync_finished.emit(results)
        return results

    def _apply(self, record, sub: dict[str, Any], now: int) -> bool:
        try:
            return self.apply_subscription(record.id, sub, now=now)
        except Exception:
            logger.warning("following backend apply failed id=%s", getattr(record, "id", "?"), exc_info=True)
            return False

    # ---- 手动导入支持 ----

    def is_enabled(self) -> bool:
        return bool(self._settings().get("enabled"))

    def list_subscriptions(self) -> list[dict[str, Any]]:
        """直连服务端拉订阅列表,供手动导入路径使用(开关由调用方把关)。"""
        return list(self._api.list_media_subscriptions())

    def apply_subscription(self, following_id: int, subscription: dict[str, Any], *, now: int | None = None) -> bool:
        """把服务端订阅信号(可播集数 + msub 绑定)并入指定本地追更记录。"""
        return bool(
            self._repository.apply_backend_signal(
                following_id,
                subscription_id=_as_int(subscription.get("id")),
                playable_episodes=_as_int(subscription.get("currentEpisodes")),
                source_key=str(getattr(self._api, "base_url", "") or ""),
                source_name="追剧",
                updated_at=now if now is not None else self._now(),
            )
        )

    def _auto_subscribe(self, record) -> dict[str, Any] | None:
        cache_key = (str(getattr(record, "provider", "") or ""), str(getattr(record, "provider_id", "") or ""))
        tried_at = self._negative_cache.get(cache_key)
        if tried_at is not None and self._now() - tried_at < NEGATIVE_CACHE_TTL_SECONDS:
            return None
        payload: dict[str, Any] = {
            "name": str(getattr(record, "title", "") or "").strip(),
            "keyword": str(getattr(record, "title", "") or "").strip(),
            "season": int(getattr(record, "season_number", 0) or 0) or 1,
        }
        if not payload["name"]:
            self._negative_cache[cache_key] = self._now()
            return None
        external_ids = {str(k): str(v) for k, v in dict(getattr(record, "external_ids", {}) or {}).items() if str(v or "").strip()}
        tmdb_id = _tmdb_series_id(external_ids.get("tmdb"))
        if tmdb_id:
            payload["metaProvider"] = "tmdb"
            payload["metaId"] = tmdb_id
        elif external_ids.get("bangumi"):
            payload["metaProvider"] = "bangumi"
            payload["metaId"] = external_ids["bangumi"]
        elif _as_int(external_ids.get("douban")):
            payload["doubanId"] = _as_int(external_ids.get("douban"))
        try:
            sub = dict(self._api.create_media_subscription(payload))
        except Exception as exc:
            logger.info("following backend auto-subscribe failed title=%s: %s", payload["name"], exc)
            self._negative_cache[cache_key] = self._now()
            return None
        if _as_int(sub.get("id")) <= 0:
            self._negative_cache[cache_key] = self._now()
            return None
        logger.info("following backend auto-subscribed title=%s subId=%s", payload["name"], sub.get("id"))
        return sub

    def _settings(self) -> dict[str, bool]:
        try:
            settings = self._settings_provider()
        except Exception:
            return {"enabled": False, "auto_subscribe": False}
        return {
            "enabled": bool(settings.get("enabled")),
            "auto_subscribe": bool(settings.get("auto_subscribe")),
        }
