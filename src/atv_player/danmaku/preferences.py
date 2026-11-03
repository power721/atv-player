from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import RLock
import time
from typing import Any, cast

from atv_player.danmaku.models import DanmakuSeriesPreference
from atv_player.danmaku.utils import infer_playlist_episode_number
from atv_player.episode_titles import extract_season_number
from atv_player.models import PlayItem
from atv_player.paths import app_data_dir


def danmaku_series_preference_path() -> Path:
    path = app_data_dir() / "danmaku-series-preferences.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def build_danmaku_tmdb_season_key(
    media_type: object,
    tmdb_id: object,
    season: object,
) -> str:
    """Build the TMDB season-level preference key for two-layer manual match memory.

    与标题级 series_key 存在同一 JSON map:``tmdb:tv:{id}:s{season}``。标题 key 由
    ``_compact_title`` 产出、不含冒号,两者天然无碰撞。身份不完整(非剧集、缺 id、
    季号缺失或非正)时返回空串——不可信身份不写全局层,防错配跨源扩散。
    """
    if str(media_type or "").strip().casefold() != "tv":
        return ""
    try:
        parsed_id = int(str(tmdb_id).strip())
        parsed_season = int(str(season).strip())
    except (TypeError, ValueError):
        return ""
    if parsed_id <= 0 or parsed_season < 1:
        return ""
    return f"tmdb:tv:{parsed_id}:s{parsed_season}"


def is_danmaku_tmdb_season_key(series_key: str) -> bool:
    return str(series_key or "").startswith("tmdb:tv:")


def _item_episode_key_season(item: PlayItem) -> int | None:
    if getattr(item, "season_number", 0):
        return int(item.season_number)
    for value in (item.original_title, item.title, item.path):
        season_number = extract_season_number(value)
        if season_number is not None:
            return season_number
    return None


def build_danmaku_episode_key(
    item: PlayItem,
    playlist: list[PlayItem] | None = None,
) -> str:
    number = infer_playlist_episode_number(item, playlist)
    if number is not None and number > 0:
        season_number = _item_episode_key_season(item)
        if season_number is not None:
            return f"s{season_number}:episode:{number}"
        return f"episode:{number}"
    label = re.sub(r"\s+", "", item.danmaku_search_episode).casefold()
    if label:
        return f"label:{label}"
    identity = str(item.vod_id or item.original_url or item.url or "").strip()
    if identity:
        digest = sha256(identity.encode("utf-8")).hexdigest()[:16]
        return f"item:{digest}"
    return "single"


def _legacy_danmaku_episode_key(episode_key: str) -> str:
    if episode_key.startswith("s") and ":episode:" in episode_key:
        return episode_key.split(":", 1)[1]
    return ""


def _normalize_persisted_offsets(value: object) -> dict[str, dict[str, float]]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, dict[str, float]] = {}
    for raw_episode_key, raw_providers in value.items():
        episode_key = str(raw_episode_key or "").strip()
        if not episode_key or not isinstance(raw_providers, dict):
            continue
        providers: dict[str, float] = {}
        for raw_provider, raw_value in raw_providers.items():
            provider = str(raw_provider or "").strip()
            try:
                offset = float(cast(Any, raw_value))
            except (TypeError, ValueError):
                continue
            if not provider or not math.isfinite(offset) or not -600.0 <= offset <= 600.0:
                continue
            if offset != 0.0:
                providers[provider] = offset
        if providers:
            output[episode_key] = providers
    return output


def _normalize_offset_for_save(value: object) -> float:
    try:
        offset = float(cast(Any, value))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(offset):
        return 0.0
    return max(-600.0, min(offset, 600.0))


class DanmakuSeriesPreferenceStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else danmaku_series_preference_path()
        self._lock = RLock()

    def load(self, series_key: str) -> DanmakuSeriesPreference | None:
        with self._lock:
            raw = self._read_all().get(series_key)
            if not isinstance(raw, dict):
                return None
            try:
                updated_at = int(cast(Any, raw.get("updated_at") or 0))
            except (TypeError, ValueError):
                updated_at = 0
            return DanmakuSeriesPreference(
                series_key=series_key,
                provider=str(raw.get("provider") or ""),
                page_url=str(raw.get("page_url") or ""),
                title=str(raw.get("title") or ""),
                search_title=str(raw.get("search_title") or ""),
                updated_at=updated_at,
                episode_source_offsets=_normalize_persisted_offsets(
                    raw.get("episode_source_offsets")
                ),
            )

    def save(self, preference: DanmakuSeriesPreference) -> DanmakuSeriesPreference:
        with self._lock:
            payload = self._read_all()
            payload[preference.series_key] = {
                "provider": preference.provider,
                "page_url": preference.page_url,
                "title": preference.title,
                "search_title": preference.search_title,
                "updated_at": preference.updated_at or int(time.time()),
                "episode_source_offsets": _normalize_persisted_offsets(
                    preference.episode_source_offsets
                ),
            }
            self._write_all(payload)
            return preference

    def load_offset(self, series_key: str, episode_key: str, provider: str) -> float:
        preference = self.load(series_key)
        if preference is None:
            return 0.0
        return preference.episode_source_offsets.get(episode_key, {}).get(provider, 0.0)

    def save_offset(
        self,
        series_key: str,
        episode_key: str,
        provider: str,
        value: float,
    ) -> None:
        series_key = str(series_key or "").strip()
        episode_key = str(episode_key or "").strip()
        provider = str(provider or "").strip()
        if not series_key or not episode_key or not provider:
            return
        offset = _normalize_offset_for_save(value)
        with self._lock:
            payload = self._read_all()
            raw = payload.get(series_key)
            if not isinstance(raw, dict):
                raw = {}
                payload[series_key] = raw
            offsets = _normalize_persisted_offsets(raw.get("episode_source_offsets"))
            providers = offsets.setdefault(episode_key, {})
            if offset == 0.0:
                providers.pop(provider, None)
                if not providers:
                    offsets.pop(episode_key, None)
            else:
                providers[provider] = offset
            raw["episode_source_offsets"] = offsets
            raw["updated_at"] = int(time.time())
            self._write_all(payload)

    def _read_all(self) -> dict[str, dict[str, object]]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_all(self, payload: dict[str, dict[str, object]]) -> None:
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
                json.dump(payload, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            temp_path.replace(self._path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


def _ensure_item_series_key(item: PlayItem) -> str:
    if item.danmaku_series_key.strip():
        return item.danmaku_series_key.strip()
    from atv_player.danmaku.service import build_danmaku_series_key

    item.danmaku_series_key = build_danmaku_series_key(
        item.media_title or item.danmaku_search_title or item.title
    )
    return item.danmaku_series_key


def merged_source_preference(
    store: DanmakuSeriesPreferenceStore | None,
    item: PlayItem,
    series_key: str | None = None,
    base: DanmakuSeriesPreference | None = None,
) -> tuple[str, DanmakuSeriesPreference | None]:
    """两层记忆读取:标题级 series 偏好叠加 TMDB 季级覆写(仅可信身份时)。

    TMDB 季级层只在 item.danmaku_tmdb_season_key 非空(播放窗口已从刮削绑定
    解析出显式 ``tv:{id}:season:{n}``)时参与;其非空字段覆盖 series 层,因为
    series_key 剥掉了季后缀、跨季共用,季级意图更精确。
    """
    series_key = series_key or _ensure_item_series_key(item)
    if store is None:
        return series_key, base
    if base is None:
        base = store.load(series_key)
    tmdb_key = str(getattr(item, "danmaku_tmdb_season_key", "") or "").strip()
    if not tmdb_key:
        return series_key, base
    overlay = store.load(tmdb_key)
    if overlay is None:
        return series_key, base
    fallbacks = (
        base
        if base is not None
        else DanmakuSeriesPreference(
            series_key=series_key, provider="", page_url="", title=""
        )
    )
    return series_key, replace(
        fallbacks,
        provider=overlay.provider or fallbacks.provider,
        page_url=overlay.page_url or fallbacks.page_url,
        title=overlay.title or fallbacks.title,
        search_title=overlay.search_title or fallbacks.search_title,
        updated_at=overlay.updated_at,
    )


def save_tmdb_season_overlay(
    store: DanmakuSeriesPreferenceStore | None,
    item: PlayItem,
    preference: DanmakuSeriesPreference,
) -> None:
    """手动选择后把同一意图覆写进 TMDB 季级层(只写手动路径,不写自动搜索)。"""
    tmdb_key = str(getattr(item, "danmaku_tmdb_season_key", "") or "").strip()
    if store is None or not tmdb_key:
        return
    existing = store.load(tmdb_key)
    store.save(
        replace(
            preference,
            series_key=tmdb_key,
            episode_source_offsets=(
                existing.episode_source_offsets if existing is not None else {}
            ),
        )
    )


def load_item_danmaku_offset(
    store: DanmakuSeriesPreferenceStore | None,
    item: PlayItem,
    playlist: list[PlayItem] | None = None,
) -> float:
    if store is None:
        return 0.0
    series_key = _ensure_item_series_key(item)
    provider = item.selected_danmaku_provider.strip()
    if not series_key or not provider:
        return 0.0
    episode_key = build_danmaku_episode_key(item, playlist)
    value = store.load_offset(series_key, episode_key, provider)
    if value != 0.0:
        return value
    legacy_key = _legacy_danmaku_episode_key(episode_key)
    if not legacy_key:
        return value
    # 旧版本偏移存的是无季前缀的 episode:{n} key;季感知 key 未命中时回退读取,
    # 避免升级后同剧老偏移孤儿。
    return store.load_offset(series_key, legacy_key, provider)


def save_item_danmaku_offset(
    store: DanmakuSeriesPreferenceStore | None,
    item: PlayItem,
    value: float,
    playlist: list[PlayItem] | None = None,
) -> float:
    if store is None:
        return 0.0
    series_key = _ensure_item_series_key(item)
    provider = item.selected_danmaku_provider.strip()
    if not series_key or not provider:
        return 0.0
    episode_key = build_danmaku_episode_key(item, playlist)
    store.save_offset(series_key, episode_key, provider, value)
    return store.load_offset(series_key, episode_key, provider)
