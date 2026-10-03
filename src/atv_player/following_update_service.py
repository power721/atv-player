# ruff: noqa: E501
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace as _replace
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from atv_player.following_metadata import compute_episode_counts
from atv_player.following_models import (
    FollowingCompletionState,
    FollowingRecord,
    FollowingUpdateResult,
    progress_after_dismissed_prompt,
    progress_at_or_beyond,
    resolve_following_completion_state,
    resolve_latest_released_season_number,
    resolve_new_episode_count,
    resolve_progress_season,
)
from atv_player.time_utils import beijing_timezone

logger = logging.getLogger(__name__)

_replace_snapshot = _replace

BEIJING_TZ = beijing_timezone()
NORMAL_INTERVAL_SECONDS = 6 * 3600
WINDOW_INTERVAL_SECONDS = 5 * 60


def is_common_update_window(timestamp: int) -> bool:
    now = datetime.fromtimestamp(timestamp, BEIJING_TZ)
    minutes = now.hour * 60 + now.minute
    return (0 <= minutes < 120) or (600 <= minutes < 780) or (1080 <= minutes < 1410)


class FollowingUpdateService(QObject):
    update_finished = Signal(object)
    _async_check_completed = Signal(list)

    def __init__(self, repository, *, metadata_gateway, now: Callable[[], int] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._repository = repository
        self._metadata_gateway = metadata_gateway
        self._now = now or (lambda: int(time.time()))
        self._timer = QTimer(self)
        # 自动轮询必须落后台线程:check_due_records 是同步网络(douban/bangumi/TMDB
        # 搜索+详情),更新窗口期 5 分钟一轮、单条可拖约 50s,QTimer 直连曾把 GUI
        # 主线程整段冻住(挂机/播放中均复现"未响应+转圈",恢复要等网络超时)。
        self._timer.timeout.connect(self._on_check_timer)
        self._async_check_running = False
        self._async_check_completed.connect(self._handle_async_check_completed)

    def next_interval_seconds(self) -> int:
        return WINDOW_INTERVAL_SECONDS if is_common_update_window(self._now()) else NORMAL_INTERVAL_SECONDS

    def start(self) -> None:
        QTimer.singleShot(60_000, self._on_check_timer)
        self._timer.start(self.next_interval_seconds() * 1000)

    def _on_check_timer(self) -> None:
        # 先重排再执行:即使本轮网络拖过下个到期点也不会漏掉后续轮询。
        self._timer.start(self.next_interval_seconds() * 1000)
        self._start_async_check()

    def _start_async_check(self) -> None:
        if self._async_check_running:
            return
        self._async_check_running = True

        def run() -> None:
            try:
                results = self._check_due_records_blocking()
            except Exception:
                logger.warning("追剧到期检查后台执行失败", exc_info=True)
                results = []
            self._async_check_completed.emit(results)

        threading.Thread(target=run, daemon=True, name="following-update-check").start()

    @Slot(list)
    def _handle_async_check_completed(self, results: list) -> None:
        self._async_check_running = False
        if results:
            self.update_finished.emit(results)

    def _check_due_records_blocking(self, limit: int = 3) -> list[FollowingUpdateResult]:
        now = self._now()
        return [
            self._check_one(record, now=now)
            for record in self._repository.load_due_records(now=now, limit=limit)
        ]

    def check_due_records(self, limit: int = 3) -> list[FollowingUpdateResult]:
        # 手动"检查更新"保持同步语义(调用方自行带 busy 态等待返回值)。
        results = self._check_due_records_blocking(limit)
        if results:
            self.update_finished.emit(results)
        if self._timer.isActive():
            self._timer.start(self.next_interval_seconds() * 1000)
        return results

    def check_record(self, record_id: int) -> FollowingUpdateResult | None:
        record = self._repository.get(record_id)
        if record is None:
            return None
        result = self._check_one(record, now=self._now())
        self.update_finished.emit([result])
        return result

    def _check_one(self, record: FollowingRecord, *, now: int) -> FollowingUpdateResult:
        last_error = ""
        record_has_tmdb_anchor = bool(record.external_ids.get("tmdb"))
        for provider in self._provider_order(record):
            if not provider:
                continue
            try:
                refreshed_record, snapshot = self._metadata_gateway.refresh(record, provider)
            except Exception as exc:
                last_error = str(exc)
                continue
            is_tmdb_fallback = record_has_tmdb_anchor and provider != "tmdb"
            raw_episodes = [
                {
                    "episode_number": episode.episode_number,
                    "type": 1 if episode.is_special else 0,
                    "air_date": episode.air_date,
                }
                for episode in snapshot.episodes
            ]
            latest_from_snapshot, total_from_snapshot = compute_episode_counts(raw_episodes, now=now)
            latest = (
                refreshed_record.latest_episode
                or latest_from_snapshot
                or record.latest_episode
            )
            completion_state = resolve_following_completion_state(
                episodes=snapshot.episodes,
                next_episode=snapshot.next_episode,
                today=datetime.fromtimestamp(now, BEIJING_TZ).date(),
            )
            total = (
                refreshed_record.total_episodes
                or total_from_snapshot
                or record.total_episodes
            )
            if (
                completion_state != FollowingCompletionState.COMPLETED
                and total > 0
                and latest > 0
                and total <= latest
            ):
                total = 0
            if is_tmdb_fallback:
                latest = max(latest, record.latest_episode)
                if record.total_episodes > 0:
                    total = max(total, record.total_episodes)
            latest_season_number = resolve_latest_released_season_number(
                record_season_number=refreshed_record.season_number or record.season_number,
                latest_episode=latest,
                current_season_number=record.current_season_number,
                episodes=snapshot.episodes,
                today=datetime.fromtimestamp(now, BEIJING_TZ).date(),
            )
            current_season_number = resolve_progress_season(
                record.current_season_number,
                record.current_episode,
                fallback_season=record.season_number,
            )
            has_update = not progress_at_or_beyond(
                current_season_number,
                record.current_episode,
                latest_season_number,
                latest,
                current_fallback_season=record.season_number,
                latest_fallback_season=latest_season_number,
            )
            new_count = resolve_new_episode_count(
                has_update=has_update,
                current_season_number=current_season_number,
                current_episode=record.current_episode,
                latest_season_number=latest_season_number,
                latest_episode=latest,
                total_episodes=total,
                seasons=snapshot.seasons,
                episodes=snapshot.episodes,
            )
            caught_up = record.watched_latest_episode or progress_at_or_beyond(
                current_season_number,
                record.current_episode,
                record.season_number,
                record.latest_episode,
                current_fallback_season=record.season_number,
                latest_fallback_season=record.season_number,
            )
            homepage_prompt = bool(
                has_update
                and caught_up
                and record.prompt_snoozed_until <= now
                and progress_after_dismissed_prompt(
                    latest_season_number,
                    latest,
                    record.prompt_dismissed_latest_season,
                    record.prompt_dismissed_latest_episode,
                    dismissed_fallback_season=record.season_number,
                    latest_fallback_season=latest_season_number,
                )
            )
            if not is_tmdb_fallback and self._has_metadata_update(refreshed_record):
                self._repository.update_metadata(record.id, refreshed_record)
            self._repository.update_check_state(
                record.id,
                latest_episode=latest,
                latest_season_number=latest_season_number,
                total_episodes=total,
                checked_at=now,
                next_check_after=now + self.next_interval_seconds(),
                has_update=has_update,
                new_episode_count=new_count,
                homepage_prompt_pending=homepage_prompt,
                last_error="",
            )
            if not is_tmdb_fallback and (snapshot.episodes or snapshot.overview or snapshot.seasons):
                if snapshot.seasons and not snapshot.episodes and not snapshot.overview:
                    existing_snapshot = self._repository.get_detail_snapshot(record.id)
                    if existing_snapshot is not None and not existing_snapshot.seasons:
                        existing_snapshot = _replace_snapshot(existing_snapshot, seasons=snapshot.seasons)
                        existing_snapshot.following_id = record.id
                        self._repository.save_detail_snapshot(record.id, existing_snapshot)
                else:
                    snapshot.following_id = record.id
                    self._repository.save_detail_snapshot(record.id, snapshot)
            return FollowingUpdateResult(
                record_id=record.id,
                checked=True,
                latest_episode=latest,
                total_episodes=total,
                has_update=has_update,
                homepage_prompt_pending=homepage_prompt,
                completion_state=completion_state,
            )
        self._repository.update_check_state(
            record.id,
            latest_episode=record.latest_episode,
            total_episodes=record.total_episodes,
            checked_at=now,
            next_check_after=now + self.next_interval_seconds(),
            has_update=record.has_update,
            new_episode_count=record.new_episode_count,
            homepage_prompt_pending=record.homepage_prompt_pending,
            last_error=last_error,
        )
        return FollowingUpdateResult(record_id=record.id, checked=False, error=last_error)

    def _provider_order(self, record: FollowingRecord) -> list[str]:
        providers: list[str] = []
        for provider in [*list(record.provider_priority or []), record.provider]:
            if provider and provider not in providers:
                providers.append(provider)
        if record.provider == "tmdb" or record.external_ids.get("tmdb"):
            return ["tmdb", *[provider for provider in providers if provider != "tmdb"]]
        media_kind = str(record.media_kind or "").strip().lower()
        should_try_bangumi_first = (
            record.provider == "bangumi"
            or "bangumi" in record.external_ids
            or any(marker in media_kind for marker in ("anime", "动漫", "动画", "番剧", "国创"))
            or (media_kind != "movie" and "bangumi" in providers)
        )
        if should_try_bangumi_first:
            providers = ["bangumi", *[provider for provider in providers if provider != "bangumi"]]
        return providers or [record.provider]

    def _has_metadata_update(self, record: FollowingRecord) -> bool:
        return any(
            (
                record.title,
                record.original_title,
                record.media_kind,
                record.poster,
                record.backdrop,
                record.rating,
                record.provider,
                record.provider_id,
                record.provider_priority,
                record.external_ids,
            )
        )
