"""源健康度学习与调度。

借鉴 WebHTV SiteHealthStore:调度排序默认开、展示排序默认关。

atv-player 的"站点"对应物:
- 播放线路级:spider 插件线路 / 内置源线路 / 网盘分享,键由
  :func:`playback_line_health_key` 派生;
- 搜索源级:全局搜索的每个源 tab,键形如 ``search:{definition.key}``。

记录成败与延迟,评分排序自动换源候选;90 天未更新的条目读时过滤并启动清理。
模块保持纯逻辑可测(计时/看门狗包装除外),UI 与窗口只做门控与调用。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Protocol

from atv_player.paths import app_data_dir

logger = logging.getLogger(__name__)

SOURCE_HEALTH_MAX_AGE_SECONDS = 90 * 24 * 60 * 60
_SOURCE_HEALTH_ERROR_MAX_CHARS = 120

HEALTH_GOOD = "good"
HEALTH_WARN = "warn"
HEALTH_BAD = "bad"
HEALTH_UNKNOWN = "unknown"

SEARCH_OUTCOME_SUCCESS = "success"
SEARCH_OUTCOME_FAIL = "fail"
SEARCH_OUTCOME_EMPTY = "empty"

PLAY_OUTCOME_SUCCESS = "success"
PLAY_OUTCOME_FAIL = "fail"

_DRIVE_SOURCE_KINDS = {"browse", "telegram", "telegram_channel", "pansou", "msub"}
_LOCAL_SEARCH_KEY_PREFIXES = ("smart", "history", "favorites", "following")


@dataclass(frozen=True, slots=True)
class SourceHealthRecord:
    key: str
    search_success: int = 0
    search_fail: int = 0
    search_empty: int = 0
    play_success: int = 0
    play_fail: int = 0
    last_search_cost_ms: int = 0
    last_search_count: int = 0
    last_play_fail_streak: int = 0
    last_error: str = ""
    updated_at: int = 0


def _classify_error(error: object) -> str:
    text = str(error or "").strip()
    if not text:
        return ""
    if "超时" in text or "timeout" in text.lower():
        return "timeout"
    if re.search(r"\b(?:4\d\d|5\d\d)\b", text):
        return "http"
    if "网络" in text or "network" in text.lower() or "connect" in text.lower():
        return "network"
    return "other"


def _truncate_error(error: object) -> str:
    text = " ".join(str(error or "").strip().split())
    if len(text) > _SOURCE_HEALTH_ERROR_MAX_CHARS:
        return text[: _SOURCE_HEALTH_ERROR_MAX_CHARS - 1] + "…"
    return text


def health_score(record: SourceHealthRecord) -> float:
    """两阶段评分:播放成败比主导,搜索成败比次之,丰富度/延迟/近期事件微调。

    无任何样本返回 0(中性,排序时保持原序);播放连续失败 3 次以上且总分
    为负时强制压到 BAD 阈值之下——播放是最终交付,连续失败比任何比率都可信。
    """
    play_total = record.play_success + record.play_fail
    search_total = record.search_success + record.search_fail + record.search_empty
    if play_total == 0 and search_total == 0:
        return 0.0
    score = 0.0
    if play_total:
        score += 60.0 * (record.play_success - record.play_fail) / (play_total + 4)
    if search_total:
        # 空结果记半失败:搜得动但没有内容,弱于真失败。
        score += (
            24.0
            * (record.search_success - record.search_fail - record.search_empty * 0.5)
            / (search_total + 4)
        )
    score += min(record.last_search_count, 20) * 0.6
    score -= min(record.last_search_cost_ms / 1000.0, 8.0)
    if record.last_play_fail_streak >= 3:
        score -= 18.0
    if record.play_success and record.last_play_fail_streak == 0 and play_total:
        score += 6.0
    if record.last_play_fail_streak >= 3 and score < 0:
        score = min(score, -21.0)
    return round(score, 3)


def health_state(record: SourceHealthRecord | None) -> str:
    if record is None:
        return HEALTH_UNKNOWN
    play_total = record.play_success + record.play_fail
    search_total = record.search_success + record.search_fail + record.search_empty
    if play_total == 0 and search_total == 0:
        return HEALTH_UNKNOWN
    score = health_score(record)
    if score >= 20:
        return HEALTH_GOOD
    if score <= -20 or (record.last_play_fail_streak >= 3 and score < 0):
        return HEALTH_BAD
    return HEALTH_WARN


def rank_candidates[T](
    candidates: list[T],
    store: SourceHealthStore | None,
    key_of: Callable[[T], str],
) -> list[T]:
    """健康排序:分高在前、无样本居中、BAD 沉底,同分保持原序(稳定)。

    store 为 None(学习关闭/测试缺省)时原样返回,不动任何顺序。
    """
    if store is None or len(candidates) <= 1:
        return list(candidates)

    def _order(entry: tuple[int, T]) -> tuple[int, float, int]:
        index, candidate = entry
        record = store.get(key_of(candidate))
        if record is None or health_state(record) == HEALTH_UNKNOWN:
            return (0, 0.0, index)
        score = health_score(record)
        if health_state(record) == HEALTH_BAD:
            # 已知坏源排到无样本之后,但不至于永远不给机会(分数仍参与同层排序)。
            return (1, -score, index)
        return (0, -score, index)

    ordered = sorted(enumerate(candidates), key=_order)
    return [candidate for _index, candidate in ordered]


def playback_line_health_key(
    source_kind: object,
    source_key: object,
    route_label: object,
    *,
    drive_share_key: object = "",
    vod_id: object = "",
) -> str:
    """播放线路级健康键:同一线路跨会话/跨条目累积成败。

    - spider_plugin → ``plugin:{manifest_id}:route:{label}``(vod_play_from 的
      线路名在同一配置内语义稳定,对应 WebHTV 的"站点");
    - 网盘类 → ``drive:{分享key}``(一个分享一个健康单元;子目录/线路不拆分);
    - 其余内置多线路源 → ``builtin:{kind}:route:{label}``;
    - 无线路信息的兜底 → ``builtin:{kind}``。
    """
    kind = str(source_kind or "").strip()
    key = str(source_key or "").strip()
    label = str(route_label or "").strip()
    if kind == "spider_plugin":
        return f"plugin:{key}:route:{label}" if label else f"plugin:{key}"
    if kind in _DRIVE_SOURCE_KINDS:
        share = str(drive_share_key or "").strip()
        if share:
            return f"drive:{share}"
        fallback = str(vod_id or "").strip()
        return f"drive:{fallback}" if fallback else f"builtin:{kind}"
    if label:
        return f"builtin:{kind}:route:{label}"
    return f"builtin:{kind}"


def search_health_key(definition_key: object) -> str:
    return f"search:{str(definition_key or '').strip()}"


def is_local_search_key(health_key: str) -> bool:
    body = health_key.removeprefix("search:")
    return body.split(":", 1)[0] in _LOCAL_SEARCH_KEY_PREFIXES


class _SearchCallable(Protocol):
    def __call__(self, keyword: str, page: int) -> tuple[list, int]: ...


def run_search_with_health(
    controller: _SearchCallable,
    keyword: str,
    page: int,
    health_key: str,
    store: SourceHealthStore | None,
    *,
    timeout_seconds: float = 25.0,
) -> tuple[list, int]:
    """执行一次源搜索:计时、健康记录与超时看门狗(Python 插件无超时的兜底)。

    本地源(智能匹配/播放记录/收藏/追更)不进看门狗线程,直接同步执行;
    远程源用单工 executor 包 future,``timeout_seconds`` 到点放弃等待并记
    timeout 失败——底层线程无法取消,泄漏一个等待中的线程优于现状(挂死
    全局搜索的"搜索中…"状态)。调用方异常语义保持:超时抛 TimeoutError,
    其余原样上抛。
    """
    if store is not None and is_local_search_key(health_key):
        store = None  # 本地查询不记健康、不看门狗
    started = time.monotonic()
    if store is None:
        return controller(keyword, page)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="src-health")
    future = executor.submit(controller, keyword, page)
    try:
        items, page_count = future.result(timeout=timeout_seconds)
    except FutureTimeoutError:
        executor.shutdown(wait=False)
        store.record_search(
            health_key,
            SEARCH_OUTCOME_FAIL,
            cost_ms=int((time.monotonic() - started) * 1000),
            count=0,
            error=f"搜索超时(>{int(timeout_seconds)}s)",
        )
        raise TimeoutError(f"源搜索超时: {health_key}") from None
    except Exception as exc:
        store.record_search(
            health_key,
            SEARCH_OUTCOME_FAIL,
            cost_ms=int((time.monotonic() - started) * 1000),
            count=0,
            error=exc,
        )
        executor.shutdown(wait=False)
        raise
    finally:
        # 成功路径:任务已完成,shutdown 立即返回;超时路径已提前 shutdown(幂等)。
        executor.shutdown(wait=False)
    cost_ms = int((time.monotonic() - started) * 1000)
    items = list(items or [])
    outcome = SEARCH_OUTCOME_SUCCESS if items else SEARCH_OUTCOME_EMPTY
    store.record_search(health_key, outcome, cost_ms=cost_ms, count=len(items))
    return items, page_count


def _normalize_record(key: str, raw: object) -> SourceHealthRecord | None:
    if not isinstance(raw, dict):
        return None

    def _int(name: str) -> int:
        try:
            return max(0, int(raw.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    return SourceHealthRecord(
        key=key,
        search_success=_int("search_success"),
        search_fail=_int("search_fail"),
        search_empty=_int("search_empty"),
        play_success=_int("play_success"),
        play_fail=_int("play_fail"),
        last_search_cost_ms=_int("last_search_cost_ms"),
        last_search_count=_int("last_search_count"),
        last_play_fail_streak=_int("last_play_fail_streak"),
        last_error=str(raw.get("last_error") or "")[:_SOURCE_HEALTH_ERROR_MAX_CHARS],
        updated_at=_int("updated_at"),
    )


class SourceHealthStore:
    """单文件 JSON 健康记录:内存权威副本 + 即时原子写 + 90 天读时过期。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else source_health_path()
        self._lock = Lock()
        self._records: dict[str, SourceHealthRecord] | None = None

    def get(self, key: str) -> SourceHealthRecord | None:
        key = str(key or "").strip()
        if not key:
            return None
        with self._lock:
            self._ensure_loaded()
            record = self._records.get(key)
            if record is None:
                return None
            if self._is_stale(record, now=int(time.time())):
                return None
            return record

    def record_search(
        self,
        key: str,
        outcome: str,
        *,
        cost_ms: int = 0,
        count: int = 0,
        error: object = "",
    ) -> None:
        key = str(key or "").strip()
        if not key:
            return

        def _mutate(record: SourceHealthRecord) -> SourceHealthRecord:
            updated = replace(
                record,
                last_search_cost_ms=max(0, int(cost_ms or 0)),
                last_search_count=max(0, int(count or 0)),
                last_error=(
                    _error_with_category(str(error or ""))
                    if outcome != SEARCH_OUTCOME_SUCCESS
                    else ""
                ),
            )
            if outcome == SEARCH_OUTCOME_SUCCESS:
                updated = replace(updated, search_success=record.search_success + 1)
            elif outcome == SEARCH_OUTCOME_EMPTY:
                updated = replace(updated, search_empty=record.search_empty + 1)
            else:
                updated = replace(updated, search_fail=record.search_fail + 1)
            return updated

        self._update(key, _mutate)

    def record_play(self, key: str, outcome: str, *, error: object = "") -> None:
        key = str(key or "").strip()
        if not key:
            return

        def _mutate(record: SourceHealthRecord) -> SourceHealthRecord:
            if outcome == PLAY_OUTCOME_SUCCESS:
                return replace(
                    record,
                    play_success=record.play_success + 1,
                    last_play_fail_streak=0,
                    last_error="",
                )
            streak = record.last_play_fail_streak + 1
            return replace(
                record,
                play_fail=record.play_fail + 1,
                last_play_fail_streak=streak,
                last_error=_error_with_category(str(error or "")),
            )

        self._update(key, _mutate)

    def purge_stale(self, now: int | None = None) -> int:
        current = int(time.time()) if now is None else int(now)
        with self._lock:
            self._ensure_loaded()
            stale_keys = [
                key
                for key, record in self._records.items()
                if self._is_stale(record, now=current)
            ]
            if not stale_keys:
                return 0
            for key in stale_keys:
                del self._records[key]
            self._write_all()
            return len(stale_keys)

    def _update(
        self,
        key: str,
        mutate: Callable[[SourceHealthRecord], SourceHealthRecord],
    ) -> None:
        with self._lock:
            self._ensure_loaded()
            base = self._records.get(key) or SourceHealthRecord(key=key)
            self._records[key] = replace(mutate(base), updated_at=int(time.time()))
            self._write_all()

    @staticmethod
    def _is_stale(record: SourceHealthRecord, *, now: int) -> bool:
        if record.updated_at <= 0:
            return True
        return now - record.updated_at > SOURCE_HEALTH_MAX_AGE_SECONDS

    def _ensure_loaded(self) -> None:
        if self._records is not None:
            return
        records: dict[str, SourceHealthRecord] = {}
        if self._path.exists():
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                for raw_key, raw_value in payload.items():
                    key = str(raw_key or "").strip()
                    if not key:
                        continue
                    record = _normalize_record(key, raw_value)
                    if record is not None:
                        records[key] = record
        self._records = records

    def _write_all(self) -> None:
        assert self._records is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                payload = {
                    key: _record_payload(record)
                    for key, record in sorted(self._records.items())
                }
                json.dump(
                    payload,
                    temp_file,
                    ensure_ascii=False,
                    indent=2,
                )
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            temp_path.replace(self._path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


def _error_with_category(error: object) -> str:
    text = " ".join(str(error or "").strip().split())
    category = _classify_error(text)
    truncated = _truncate_error(text)
    if not truncated:
        return category
    if not category or truncated.startswith("["):
        return truncated
    return _truncate_error(f"[{category}] {truncated}")


def _record_payload(record: SourceHealthRecord) -> dict[str, object]:
    return {
        "search_success": record.search_success,
        "search_fail": record.search_fail,
        "search_empty": record.search_empty,
        "play_success": record.play_success,
        "play_fail": record.play_fail,
        "last_search_cost_ms": record.last_search_cost_ms,
        "last_search_count": record.last_search_count,
        "last_play_fail_streak": record.last_play_fail_streak,
        "last_error": record.last_error,
        "updated_at": record.updated_at,
    }


def source_health_path() -> Path:
    path = app_data_dir() / "source-health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def purge_stale_source_health(now: int | None = None) -> None:
    """启动清理入口(app.py 起 daemon thread 调用);异常只记日志。"""
    try:
        SourceHealthStore().purge_stale(now=now)
    except Exception:
        logger.warning("Purge stale source health failed", exc_info=True)
