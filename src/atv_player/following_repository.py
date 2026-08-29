# ruff: noqa: E501
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from atv_player.following_models import (
    FollowingDetailSnapshot,
    FollowingEpisode,
    FollowingMetadataBundle,
    FollowingMetadataSourceSnapshot,
    FollowingPlaybackPlatformEntry,
    FollowingRatingEntry,
    FollowingRecord,
    FollowingSeason,
    FollowingSourceBinding,
    progress_after_dismissed_prompt,
    progress_at_or_beyond,
    resolve_progress_season,
)
from atv_player.sqlite_utils import managed_connection


def _tmdb_series_provider_id(provider_id: object) -> str:
    text = str(provider_id or "").strip()
    if not text.startswith("tv:"):
        return text
    return text.split(":season:", 1)[0]


def _tmdb_season_number_from_provider_id(provider_id: object) -> int:
    text = str(provider_id or "").strip()
    if ":season:" not in text:
        return 0
    try:
        return int(text.rsplit(":season:", 1)[1])
    except (TypeError, ValueError):
        return 0


def _canonical_provider_id(provider: object, provider_id: object) -> str:
    text = str(provider_id or "").strip()
    return _tmdb_series_provider_id(text) if str(provider or "").strip() == "tmdb" else text


def _normalize_record_identity(record: FollowingRecord) -> FollowingRecord:
    canonical_provider_id = _canonical_provider_id(record.provider, record.provider_id)
    if canonical_provider_id == record.provider_id and not (
        record.provider == "tmdb" and record.season_number <= 0 and _tmdb_season_number_from_provider_id(record.provider_id) > 0
    ):
        return record
    inferred_season = record.season_number or _tmdb_season_number_from_provider_id(record.provider_id)
    return replace(
        record,
        provider_id=canonical_provider_id,
        season_number=inferred_season,
    )


def _json_loads(value: object, fallback: Any) -> Any:
    try:
        loaded = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return loaded if loaded is not None else fallback


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _binding_to_dict(binding: FollowingSourceBinding) -> dict[str, str]:
    return {
        "source_kind": binding.source_kind,
        "source_key": binding.source_key,
        "source_name": binding.source_name,
        "vod_id": binding.vod_id,
        "provider": binding.provider,
        "provider_id": binding.provider_id,
    }


def _binding_from_dict(value: object) -> FollowingSourceBinding:
    data = value if isinstance(value, dict) else {}
    return FollowingSourceBinding(
        source_kind=str(data.get("source_kind") or ""),
        source_key=str(data.get("source_key") or ""),
        source_name=str(data.get("source_name") or ""),
        vod_id=str(data.get("vod_id") or ""),
        provider=str(data.get("provider") or ""),
        provider_id=str(data.get("provider_id") or ""),
    )


def _episode_to_dict(episode: FollowingEpisode) -> dict[str, object]:
    return {
        "episode_number": episode.episode_number,
        "season_number": episode.season_number,
        "title": episode.title,
        "overview": episode.overview,
        "air_date": episode.air_date,
        "still": episode.still,
        "runtime": episode.runtime,
        "is_special": episode.is_special,
    }


def _episode_from_dict(value: object) -> FollowingEpisode:
    data = value if isinstance(value, dict) else {}
    return FollowingEpisode(
        episode_number=int(data.get("episode_number") or 0),
        season_number=int(data.get("season_number") or 0),
        title=str(data.get("title") or ""),
        overview=str(data.get("overview") or ""),
        air_date=str(data.get("air_date") or ""),
        still=str(data.get("still") or ""),
        runtime=int(data.get("runtime") or 0),
        is_special=bool(data.get("is_special", False)),
    )


def _season_to_dict(season: FollowingSeason) -> dict[str, object]:
    return {
        "season_number": season.season_number,
        "title": season.title,
        "overview": season.overview,
        "air_date": season.air_date,
        "poster": season.poster,
        "episode_count": season.episode_count,
        "is_special": season.is_special,
    }


def _season_from_dict(value: object) -> FollowingSeason:
    data = value if isinstance(value, dict) else {}
    return FollowingSeason(
        season_number=int(data.get("season_number") or 0),
        title=str(data.get("title") or ""),
        overview=str(data.get("overview") or ""),
        air_date=str(data.get("air_date") or ""),
        poster=str(data.get("poster") or ""),
        episode_count=int(data.get("episode_count") or 0),
        is_special=bool(data.get("is_special", False)),
    )


def _rating_entry_to_dict(entry: FollowingRatingEntry) -> dict[str, str]:
    return {
        "provider": entry.provider,
        "label": entry.label,
        "value": entry.value,
    }


def _rating_entry_from_dict(value: object) -> FollowingRatingEntry:
    data = value if isinstance(value, dict) else {}
    return FollowingRatingEntry(
        provider=str(data.get("provider") or ""),
        label=str(data.get("label") or ""),
        value=str(data.get("value") or ""),
    )


def _platform_entry_to_dict(entry: FollowingPlaybackPlatformEntry) -> dict[str, object]:
    data = {
        "provider": entry.provider,
        "label": entry.label,
        "url": entry.url,
        "latest_episode": entry.latest_episode,
        "update_time_text": entry.update_time_text,
        "status_text": entry.status_text,
    }
    if entry.metric_label:
        data["metric_label"] = entry.metric_label
    if entry.metric_value:
        data["metric_value"] = entry.metric_value
    return data


def _platform_entry_from_dict(value: object) -> FollowingPlaybackPlatformEntry:
    data = value if isinstance(value, dict) else {}
    return FollowingPlaybackPlatformEntry(
        provider=str(data.get("provider") or ""),
        label=str(data.get("label") or ""),
        url=str(data.get("url") or ""),
        latest_episode=int(data.get("latest_episode") or 0),
        update_time_text=str(data.get("update_time_text") or ""),
        status_text=str(data.get("status_text") or ""),
        metric_label=str(data.get("metric_label") or ""),
        metric_value=str(data.get("metric_value") or ""),
    )


def _source_snapshot_to_dict(snapshot: FollowingMetadataSourceSnapshot) -> dict[str, object]:
    return {
        "source_key": snapshot.source_key,
        "provider": snapshot.provider,
        "provider_label": snapshot.provider_label,
        "provider_id": snapshot.provider_id,
        "matched": snapshot.matched,
        "confidence": snapshot.confidence,
        "url": snapshot.url,
        "overview": snapshot.overview,
        "metadata_fields": list(snapshot.metadata_fields),
        "ratings": [_rating_entry_to_dict(entry) for entry in snapshot.ratings],
        "playback_platforms": [_platform_entry_to_dict(entry) for entry in snapshot.playback_platforms],
        "episodes": [_episode_to_dict(item) for item in snapshot.episodes],
        "seasons": [_season_to_dict(item) for item in snapshot.seasons],
    }


def _source_snapshot_from_dict(value: object) -> FollowingMetadataSourceSnapshot:
    data = value if isinstance(value, dict) else {}
    metadata_fields = []
    for item in data.get("metadata_fields") or []:
        if not isinstance(item, dict):
            continue
        metadata_fields.append(
            {
                "label": str(item.get("label") or ""),
                "value": str(item.get("value") or ""),
            }
        )
    return FollowingMetadataSourceSnapshot(
        source_key=str(data.get("source_key") or ""),
        provider=str(data.get("provider") or ""),
        provider_label=str(data.get("provider_label") or ""),
        provider_id=str(data.get("provider_id") or ""),
        matched=bool(data.get("matched", True)),
        confidence=float(data.get("confidence") or 0.0),
        url=str(data.get("url") or ""),
        overview=str(data.get("overview") or ""),
        metadata_fields=metadata_fields,
        ratings=[_rating_entry_from_dict(item) for item in data.get("ratings") or []],
        playback_platforms=[_platform_entry_from_dict(item) for item in data.get("playback_platforms") or []],
        episodes=[_episode_from_dict(item) for item in data.get("episodes") or []],
        seasons=[_season_from_dict(item) for item in data.get("seasons") or []],
    )


def _metadata_bundle_to_dict(bundle: FollowingMetadataBundle | None) -> dict[str, object] | None:
    if bundle is None:
        return None
    return {
        "merged_snapshot": _source_snapshot_to_dict(bundle.merged_snapshot),
        "source_snapshots": {
            str(key): _source_snapshot_to_dict(snapshot)
            for key, snapshot in bundle.source_snapshots.items()
        },
        "available_source_keys": list(bundle.available_source_keys),
        "default_source_key": bundle.default_source_key,
    }


def _metadata_bundle_from_dict(value: object) -> FollowingMetadataBundle | None:
    data = value if isinstance(value, dict) else {}
    merged_raw = data.get("merged_snapshot")
    if not isinstance(merged_raw, dict):
        return None
    source_snapshots: dict[str, FollowingMetadataSourceSnapshot] = {}
    raw_sources = data.get("source_snapshots")
    if isinstance(raw_sources, dict):
        for key, snapshot in raw_sources.items():
            if not isinstance(snapshot, dict):
                continue
            source_snapshots[str(key)] = _source_snapshot_from_dict(snapshot)
    merged_snapshot = _source_snapshot_from_dict(merged_raw)
    if "merged" not in source_snapshots:
        source_snapshots["merged"] = merged_snapshot
    available_source_keys = [str(item) for item in data.get("available_source_keys") or [] if str(item or "").strip()]
    if not available_source_keys:
        available_source_keys = list(source_snapshots) or ["merged"]
    default_source_key = str(data.get("default_source_key") or "merged").strip() or "merged"
    return FollowingMetadataBundle(
        merged_snapshot=merged_snapshot,
        source_snapshots=source_snapshots,
        available_source_keys=available_source_keys,
        default_source_key=default_source_key,
    )


class FollowingRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        return managed_connection(self._db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS following (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    original_title TEXT NOT NULL DEFAULT '',
                    media_kind TEXT NOT NULL DEFAULT '',
                    season_number INTEGER NOT NULL DEFAULT 0,
                    poster TEXT NOT NULL DEFAULT '',
                    backdrop TEXT NOT NULL DEFAULT '',
                    rating TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    provider_id TEXT NOT NULL DEFAULT '',
                    provider_priority_json TEXT NOT NULL DEFAULT '[]',
                    external_ids_json TEXT NOT NULL DEFAULT '{}',
                    source_bindings_json TEXT NOT NULL DEFAULT '[]',
                    current_season_number INTEGER NOT NULL DEFAULT 0,
                    current_episode INTEGER NOT NULL DEFAULT 0,
                    position_seconds INTEGER NOT NULL DEFAULT 0,
                    watched_latest_episode INTEGER NOT NULL DEFAULT 0,
                    latest_episode INTEGER NOT NULL DEFAULT 0,
                    previous_latest_episode INTEGER NOT NULL DEFAULT 0,
                    total_episodes INTEGER NOT NULL DEFAULT 0,
                    has_update INTEGER NOT NULL DEFAULT 0,
                    new_episode_count INTEGER NOT NULL DEFAULT 0,
                    homepage_prompt_pending INTEGER NOT NULL DEFAULT 0,
                    prompt_snoozed_until INTEGER NOT NULL DEFAULT 0,
                    prompt_dismissed_latest_episode INTEGER NOT NULL DEFAULT 0,
                    prompt_dismissed_latest_season INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    last_played_at INTEGER NOT NULL DEFAULT 0,
                    last_checked_at INTEGER NOT NULL DEFAULT 0,
                    next_check_after INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(provider, provider_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS following_detail_snapshots (
                    following_id INTEGER PRIMARY KEY,
                    overview TEXT NOT NULL DEFAULT '',
                    metadata_fields_json TEXT NOT NULL DEFAULT '[]',
                    cast_json TEXT NOT NULL DEFAULT '[]',
                    crew_json TEXT NOT NULL DEFAULT '[]',
                    seasons_json TEXT NOT NULL DEFAULT '[]',
                    episodes_json TEXT NOT NULL DEFAULT '[]',
                    posters_json TEXT NOT NULL DEFAULT '[]',
                    backdrops_json TEXT NOT NULL DEFAULT '[]',
                    metadata_bundle_json TEXT NOT NULL DEFAULT '',
                    refreshed_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            try:
                conn.execute("ALTER TABLE following_detail_snapshots ADD COLUMN metadata_fields_json TEXT NOT NULL DEFAULT '[]'")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE following ADD COLUMN current_season_number INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE following_detail_snapshots ADD COLUMN seasons_json TEXT NOT NULL DEFAULT '[]'")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE following_detail_snapshots ADD COLUMN metadata_bundle_json TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE following ADD COLUMN prompt_dismissed_latest_episode INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE following ADD COLUMN prompt_dismissed_latest_season INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
            conn.execute(
                """
                UPDATE following
                SET prompt_dismissed_latest_season = CASE
                    WHEN season_number > 0 THEN season_number
                    ELSE 1
                END
                WHERE prompt_dismissed_latest_episode > 0
                  AND prompt_dismissed_latest_season = 0
                """
            )
            conn.execute(
                """
                UPDATE following
                SET current_season_number = CASE
                    WHEN current_episode > 0 AND season_number > 0 THEN season_number
                    WHEN current_episode > 0 THEN 1
                    ELSE 0
                END
                WHERE current_season_number = 0
                """
            )
            self._migrate_tmdb_series_provider_ids(conn)

    def upsert(self, record: FollowingRecord) -> int:
        record = _normalize_record_identity(record)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO following (
                    title, original_title, media_kind, season_number, poster, backdrop, rating,
                    provider, provider_id, provider_priority_json, external_ids_json, source_bindings_json,
                    current_season_number, current_episode, position_seconds, watched_latest_episode, latest_episode,
                    previous_latest_episode, total_episodes, has_update, new_episode_count,
                    homepage_prompt_pending, prompt_snoozed_until, prompt_dismissed_latest_episode,
                    prompt_dismissed_latest_season, created_at, updated_at,
                    last_played_at, last_checked_at, next_check_after, last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, provider_id) DO UPDATE SET
                    title = excluded.title,
                    original_title = excluded.original_title,
                    media_kind = excluded.media_kind,
                    season_number = excluded.season_number,
                    poster = excluded.poster,
                    backdrop = excluded.backdrop,
                    rating = excluded.rating,
                    provider_priority_json = excluded.provider_priority_json,
                    external_ids_json = excluded.external_ids_json,
                    source_bindings_json = excluded.source_bindings_json,
                    current_season_number = excluded.current_season_number,
                    current_episode = excluded.current_episode,
                    position_seconds = excluded.position_seconds,
                    watched_latest_episode = excluded.watched_latest_episode,
                    latest_episode = excluded.latest_episode,
                    previous_latest_episode = excluded.previous_latest_episode,
                    total_episodes = excluded.total_episodes,
                    has_update = excluded.has_update,
                    new_episode_count = excluded.new_episode_count,
                    homepage_prompt_pending = excluded.homepage_prompt_pending,
                    prompt_snoozed_until = excluded.prompt_snoozed_until,
                    prompt_dismissed_latest_episode = excluded.prompt_dismissed_latest_episode,
                    prompt_dismissed_latest_season = excluded.prompt_dismissed_latest_season,
                    updated_at = excluded.updated_at,
                    last_played_at = excluded.last_played_at,
                    last_checked_at = excluded.last_checked_at,
                    next_check_after = excluded.next_check_after,
                    last_error = excluded.last_error
                """,
                self._record_params(record),
            )
            if cursor.lastrowid:
                return int(cursor.lastrowid)
            row = conn.execute(
                "SELECT id FROM following WHERE provider = ? AND provider_id = ?",
                (record.provider, record.provider_id),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def get(self, record_id: int) -> FollowingRecord | None:
        with self._connect() as conn:
            row = conn.execute(f"{self._select_sql()} WHERE id = ?", (record_id,)).fetchone()
        return self._record_from_row(row) if row is not None else None

    def get_by_identity(self, provider: str, provider_id: str) -> FollowingRecord | None:
        provider_id = _canonical_provider_id(provider, provider_id)
        with self._connect() as conn:
            row = conn.execute(
                f"{self._select_sql()} WHERE provider = ? AND provider_id = ?",
                (provider, provider_id),
            ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def load_page(self, *, page: int, size: int, keyword: str, only_updates: bool) -> tuple[list[FollowingRecord], int]:
        clauses: list[str] = []
        params: list[object] = []
        normalized_keyword = keyword.strip()
        if normalized_keyword:
            clauses.append("(title LIKE ? OR original_title LIKE ?)")
            like = f"%{normalized_keyword}%"
            params.extend([like, like])
        if only_updates:
            clauses.append("has_update = 1")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        offset = max(page - 1, 0) * size
        with self._connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM following {where_sql}", params).fetchone()[0])
            rows = conn.execute(
                f"""
                {self._select_sql()}
                {where_sql}
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, size, offset],
            ).fetchall()
        return [self._record_from_row(row) for row in rows], total

    def delete(self, record_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM following_detail_snapshots WHERE following_id = ?", (record_id,))
            conn.execute("DELETE FROM following WHERE id = ?", (record_id,))

    def save_detail_snapshot(self, following_id: int, snapshot: FollowingDetailSnapshot) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO following_detail_snapshots (
                    following_id, overview, metadata_fields_json, cast_json, crew_json, seasons_json, episodes_json,
                    posters_json, backdrops_json, metadata_bundle_json, refreshed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(following_id) DO UPDATE SET
                    overview = excluded.overview,
                    metadata_fields_json = excluded.metadata_fields_json,
                    cast_json = excluded.cast_json,
                    crew_json = excluded.crew_json,
                    seasons_json = excluded.seasons_json,
                    episodes_json = excluded.episodes_json,
                    posters_json = excluded.posters_json,
                    backdrops_json = excluded.backdrops_json,
                    metadata_bundle_json = excluded.metadata_bundle_json,
                    refreshed_at = excluded.refreshed_at
                """,
                (
                    following_id,
                    snapshot.overview,
                    _json_dumps(snapshot.metadata_fields),
                    _json_dumps(snapshot.cast),
                    _json_dumps(snapshot.crew),
                    _json_dumps([_season_to_dict(season) for season in snapshot.seasons]),
                    _json_dumps([_episode_to_dict(episode) for episode in snapshot.episodes]),
                    _json_dumps(snapshot.posters),
                    _json_dumps(snapshot.backdrops),
                    _json_dumps(_metadata_bundle_to_dict(snapshot.metadata_bundle) or {}),
                    snapshot.refreshed_at,
                ),
            )

    def get_detail_snapshot(self, following_id: int) -> FollowingDetailSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT following_id, overview, metadata_fields_json, cast_json, crew_json, seasons_json, episodes_json,
                       posters_json, backdrops_json, metadata_bundle_json, refreshed_at
                FROM following_detail_snapshots
                WHERE following_id = ?
                """,
                (following_id,),
            ).fetchone()
        if row is None:
            return None
        return FollowingDetailSnapshot(
            following_id=int(row[0]),
            overview=str(row[1]),
            metadata_fields=[
                {"label": str(item.get("label") or ""), "value": str(item.get("value") or "")}
                for item in _json_loads(row[2], [])
                if isinstance(item, dict)
            ],
            cast=list(_json_loads(row[3], [])),
            crew=list(_json_loads(row[4], [])),
            seasons=[_season_from_dict(item) for item in _json_loads(row[5], [])],
            episodes=[_episode_from_dict(item) for item in _json_loads(row[6], [])],
            posters=[str(item) for item in _json_loads(row[7], [])],
            backdrops=[str(item) for item in _json_loads(row[8], [])],
            metadata_bundle=_metadata_bundle_from_dict(_json_loads(row[9], {})),
            refreshed_at=int(row[10]),
        )

    def update_progress(
        self,
        following_id: int,
        *,
        current_season_number: int = 0,
        current_episode: int,
        position_seconds: int,
        last_played_at: int,
    ) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT season_number, latest_episode FROM following WHERE id = ?",
                (following_id,),
            ).fetchone()
            if row is None:
                return
            latest_season_number = resolve_progress_season(
                int(row[0] or 0),
                int(row[1] or 0),
            )
            latest_episode = int(row[1] or 0)
            normalized_current_season = resolve_progress_season(
                current_season_number,
                current_episode,
                fallback_season=latest_season_number,
            )
            watched_latest = progress_at_or_beyond(
                normalized_current_season,
                current_episode,
                latest_season_number,
                latest_episode,
            )
            conn.execute(
                """
                UPDATE following
                SET current_season_number = ?, current_episode = ?, position_seconds = ?, last_played_at = ?,
                    watched_latest_episode = ?,
                    has_update = CASE WHEN ? THEN 0 ELSE has_update END,
                    new_episode_count = CASE WHEN ? THEN 0 ELSE new_episode_count END,
                    homepage_prompt_pending = CASE WHEN ? THEN 0 ELSE homepage_prompt_pending END
                WHERE id = ?
                """,
                (
                    normalized_current_season,
                    current_episode,
                    position_seconds,
                    last_played_at,
                    1 if watched_latest else 0,
                    1 if watched_latest else 0,
                    1 if watched_latest else 0,
                    1 if watched_latest else 0,
                    following_id,
                ),
            )

    def update_playback_source_state(
        self,
        following_id: int,
        *,
        source_bindings: list[FollowingSourceBinding],
        latest_episode: int,
        updated_at: int,
    ) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT latest_episode, current_episode FROM following WHERE id = ?",
                (following_id,),
            ).fetchone()
            if row is None:
                return
            previous_latest = int(row[0] or 0)
            current_episode = int(row[1] or 0)
            normalized_latest = max(previous_latest, int(latest_episode or 0))
            latest_advanced = normalized_latest > previous_latest
            has_update = normalized_latest > current_episode
            new_episode_count = max(0, normalized_latest - current_episode)
            conn.execute(
                """
                UPDATE following
                SET source_bindings_json = ?, latest_episode = ?,
                    has_update = CASE WHEN ? THEN 1 ELSE has_update END,
                    new_episode_count = CASE WHEN ? THEN ? ELSE new_episode_count END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    _json_dumps([_binding_to_dict(binding) for binding in source_bindings]),
                    normalized_latest,
                    1 if has_update else 0,
                    1 if latest_advanced else 0,
                    new_episode_count,
                    updated_at,
                    following_id,
                ),
            )

    def apply_backend_signal(
        self,
        following_id: int,
        *,
        subscription_id: int,
        playable_episodes: int,
        source_key: str = "",
        source_name: str = "",
        updated_at: int,
    ) -> bool:
        """服务端追剧信号并入:latest 取 max、upsert msub 绑定、按需抬首页提示。

        必须在元数据巡检落库之后调用——update_check_state 对 latest_episode 是直写覆盖,
        本方法用 max 语义兜住"服务端资源先于官方元数据可播"的抬升不被压回。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT season_number, latest_episode, current_season_number, current_episode, watched_latest_episode, prompt_snoozed_until, prompt_dismissed_latest_episode, prompt_dismissed_latest_season, source_bindings_json FROM following WHERE id = ?",
                (following_id,),
            ).fetchone()
            if row is None:
                return False
            season_number = int(row[0] or 0)
            previous_latest = int(row[1] or 0)
            current_season_number = int(row[2] or 0)
            current_episode = int(row[3] or 0)
            watched_latest = bool(row[4])
            prompt_snoozed_until = int(row[5] or 0)
            dismissed_latest_episode = int(row[6] or 0)
            dismissed_latest_season = int(row[7] or 0)
            bindings = [_binding_from_dict(item) for item in _json_loads(row[8], [])]
            normalized_latest = max(previous_latest, int(playable_episodes or 0))
            latest_advanced = normalized_latest > previous_latest
            has_update = normalized_latest > current_episode
            new_episode_count = max(0, normalized_latest - current_episode)
            vod_id = f"msub:{int(subscription_id)}"
            bindings = [binding for binding in bindings if not (binding.source_kind == "msub" and binding.vod_id != vod_id)]
            if not any(binding.source_kind == "msub" and binding.vod_id == vod_id for binding in bindings):
                # 插到列表头:"继续播放"取首个绑定,服务端可播源就绪即成为默认续播源;
                # 用户之后实际播放的源会被 record_playback_source 的 insert(0) 顶回首位。
                bindings.insert(
                    0,
                    FollowingSourceBinding(
                        source_kind="msub",
                        source_key=str(source_key or ""),
                        source_name=str(source_name or "追剧"),
                        vod_id=vod_id,
                    )
                )
            caught_up = watched_latest or progress_at_or_beyond(
                current_season_number,
                current_episode,
                season_number,
                normalized_latest,
                current_fallback_season=season_number,
                latest_fallback_season=season_number,
            )
            homepage_prompt = bool(
                has_update
                and caught_up
                and prompt_snoozed_until <= updated_at
                and progress_after_dismissed_prompt(
                    season_number,
                    normalized_latest,
                    dismissed_latest_season,
                    dismissed_latest_episode,
                    dismissed_fallback_season=season_number,
                    latest_fallback_season=season_number,
                )
            )
            conn.execute(
                """
                UPDATE following
                SET source_bindings_json = ?, latest_episode = ?,
                    has_update = CASE WHEN ? THEN 1 ELSE has_update END,
                    new_episode_count = CASE WHEN ? THEN ? ELSE new_episode_count END,
                    homepage_prompt_pending = CASE WHEN ? THEN 1 ELSE homepage_prompt_pending END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    _json_dumps([_binding_to_dict(binding) for binding in bindings]),
                    normalized_latest,
                    1 if has_update else 0,
                    1 if latest_advanced else 0,
                    new_episode_count,
                    1 if homepage_prompt else 0,
                    updated_at,
                    following_id,
                ),
            )
        return latest_advanced or homepage_prompt

    def update_check_state(
        self,
        following_id: int,
        *,
        latest_episode: int,
        total_episodes: int,
        checked_at: int,
        next_check_after: int,
        has_update: bool,
        new_episode_count: int,
        homepage_prompt_pending: bool,
        last_error: str,
        latest_season_number: int = 0,
    ) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT season_number, latest_episode, current_season_number, current_episode, prompt_dismissed_latest_episode, prompt_dismissed_latest_season FROM following WHERE id = ?",
                (following_id,),
            ).fetchone()
            if row is None:
                return
            previous_season_number = int(row[0] or 0)
            previous = int(row[1] or 0)
            current_season_number = int(row[2] or 0)
            current_episode = int(row[3] or 0)
            dismissed_latest_episode = int(row[4] or 0)
            dismissed_latest_season = int(row[5] or 0)
            resolved_latest_season = int(latest_season_number or 0) or previous_season_number
            watched_latest = progress_at_or_beyond(
                current_season_number,
                current_episode,
                resolved_latest_season,
                latest_episode,
                current_fallback_season=previous_season_number,
                latest_fallback_season=resolved_latest_season,
            )
            effective_homepage_prompt_pending = bool(
                homepage_prompt_pending
                and progress_after_dismissed_prompt(
                    resolved_latest_season,
                    latest_episode,
                    dismissed_latest_season,
                    dismissed_latest_episode,
                    dismissed_fallback_season=previous_season_number,
                    latest_fallback_season=resolved_latest_season,
                )
            )
            conn.execute(
                """
                UPDATE following
                SET previous_latest_episode = ?, latest_episode = ?, total_episodes = ?,
                    last_checked_at = ?, next_check_after = ?, has_update = ?, new_episode_count = ?,
                    homepage_prompt_pending = ?, watched_latest_episode = ?, last_error = ?
                WHERE id = ?
                """,
                (
                    previous,
                    latest_episode,
                    total_episodes,
                    checked_at,
                    next_check_after,
                    1 if has_update else 0,
                    new_episode_count,
                    1 if effective_homepage_prompt_pending else 0,
                    1 if watched_latest else 0,
                    last_error,
                    following_id,
                ),
            )

    def update_metadata(self, following_id: int, record: FollowingRecord) -> None:
        record = _normalize_record_identity(record)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE following
                SET title = ?, original_title = ?, media_kind = ?, season_number = ?,
                    poster = ?, backdrop = ?, rating = ?, provider = ?, provider_id = ?,
                    provider_priority_json = ?, external_ids_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    record.title,
                    record.original_title,
                    record.media_kind,
                    record.season_number,
                    record.poster,
                    record.backdrop,
                    record.rating,
                    record.provider,
                    record.provider_id,
                    _json_dumps(record.provider_priority),
                    _json_dumps(record.external_ids),
                    record.updated_at,
                    following_id,
                ),
            )

    def _migrate_tmdb_series_provider_ids(self, conn) -> None:
        rows = conn.execute(
            f"""
            {self._select_sql()}
            WHERE provider = 'tmdb' AND provider_id LIKE 'tv:%'
            ORDER BY updated_at DESC, created_at DESC, id DESC
            """
        ).fetchall()
        if not rows:
            return
        grouped: dict[str, list[FollowingRecord]] = {}
        for row in rows:
            record = self._record_from_row(row)
            grouped.setdefault(_tmdb_series_provider_id(record.provider_id), []).append(record)
        for canonical_provider_id, records in grouped.items():
            if not canonical_provider_id:
                continue
            primary = records[0]
            duplicates = records[1:]
            merged = _normalize_record_identity(primary)
            dismissed_record = max(records, key=self._prompt_dismissed_progress_key)
            merged = replace(
                merged,
                external_ids=self._merge_string_maps(record.external_ids for record in records),
                provider_priority=self._merge_string_lists(record.provider_priority for record in records),
                source_bindings=self._merge_source_bindings(record.source_bindings for record in records),
                latest_episode=max(record.latest_episode for record in records),
                previous_latest_episode=max(record.previous_latest_episode for record in records),
                total_episodes=max(record.total_episodes for record in records),
                has_update=any(record.has_update for record in records),
                new_episode_count=max(record.new_episode_count for record in records),
                homepage_prompt_pending=any(record.homepage_prompt_pending for record in records),
                prompt_dismissed_latest_episode=dismissed_record.prompt_dismissed_latest_episode,
                prompt_dismissed_latest_season=dismissed_record.prompt_dismissed_latest_season,
                season_number=max(record.season_number or _tmdb_season_number_from_provider_id(record.provider_id) for record in records),
            )
            if not merged.last_error:
                merged = replace(
                    merged,
                    last_error=next((record.last_error for record in records if record.last_error), ""),
                )
            if duplicates:
                primary_snapshot_exists = (
                    conn.execute(
                        "SELECT 1 FROM following_detail_snapshots WHERE following_id = ?",
                        (primary.id,),
                    ).fetchone()
                    is not None
                )
                if not primary_snapshot_exists:
                    for duplicate in duplicates:
                        duplicate_snapshot_exists = (
                            conn.execute(
                                "SELECT 1 FROM following_detail_snapshots WHERE following_id = ?",
                                (duplicate.id,),
                            ).fetchone()
                            is not None
                        )
                        if duplicate_snapshot_exists:
                            conn.execute(
                                "UPDATE following_detail_snapshots SET following_id = ? WHERE following_id = ?",
                                (primary.id, duplicate.id),
                            )
                            break
                for duplicate in duplicates:
                    conn.execute("DELETE FROM following_detail_snapshots WHERE following_id = ?", (duplicate.id,))
                    conn.execute("DELETE FROM following WHERE id = ?", (duplicate.id,))
            conn.execute(
                """
                UPDATE following
                SET title = ?, original_title = ?, media_kind = ?, season_number = ?,
                    poster = ?, backdrop = ?, rating = ?, provider = ?, provider_id = ?,
                    provider_priority_json = ?, external_ids_json = ?, source_bindings_json = ?,
                    current_season_number = ?, current_episode = ?, position_seconds = ?, watched_latest_episode = ?, latest_episode = ?,
                    previous_latest_episode = ?, total_episodes = ?, has_update = ?, new_episode_count = ?,
                    homepage_prompt_pending = ?, prompt_snoozed_until = ?, prompt_dismissed_latest_episode = ?,
                    prompt_dismissed_latest_season = ?, created_at = ?, updated_at = ?,
                    last_played_at = ?, last_checked_at = ?, next_check_after = ?, last_error = ?
                WHERE id = ?
                """,
                (*self._record_params(merged), primary.id),
            )

    @staticmethod
    def _prompt_dismissed_progress_key(record: FollowingRecord) -> tuple[int, int]:
        episode = max(0, int(record.prompt_dismissed_latest_episode or 0))
        if episode <= 0:
            return (0, 0)
        season = resolve_progress_season(
            record.prompt_dismissed_latest_season,
            episode,
            fallback_season=record.season_number,
        )
        return (season, episode)

    @staticmethod
    def _merge_string_maps(maps) -> dict[str, str]:
        merged: dict[str, str] = {}
        for mapping in maps:
            merged.update({str(key): str(value) for key, value in dict(mapping).items() if str(value)})
        return merged

    @staticmethod
    def _merge_string_lists(lists) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for values in lists:
            for value in values:
                text = str(value or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                merged.append(text)
        return merged

    @staticmethod
    def _merge_source_bindings(binding_lists) -> list[FollowingSourceBinding]:
        merged: list[FollowingSourceBinding] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for bindings in binding_lists:
            for binding in bindings:
                key = (
                    binding.source_kind,
                    binding.source_key,
                    binding.vod_id,
                    binding.provider,
                    binding.provider_id,
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(binding)
        return merged

    def load_due_records(self, *, now: int, limit: int) -> list[FollowingRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                {self._select_sql()}
                WHERE next_check_after <= ?
                ORDER BY next_check_after ASC, updated_at DESC, id ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def load_recent_recommendation_candidates(self, *, limit: int) -> list[FollowingRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                {self._select_sql()}
                WHERE provider = 'tmdb' OR external_ids_json LIKE '%"tmdb"%'
                ORDER BY has_update DESC, last_played_at DESC, updated_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
                (max(0, int(limit or 0)),),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def load_homepage_prompt_records(self, *, now: int) -> list[FollowingRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                {self._select_sql()}
                WHERE homepage_prompt_pending = 1 AND prompt_snoozed_until <= ?
                ORDER BY updated_at DESC, id ASC
                """,
                (now,),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def clear_homepage_prompt(self, following_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE following SET homepage_prompt_pending = 0 WHERE id = ?", (following_id,))

    def snooze_prompt(self, following_id: int, *, until: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE following SET homepage_prompt_pending = 0, prompt_snoozed_until = ? WHERE id = ?",
                (until, following_id),
            )

    def dismiss_prompt_until_next_episode(self, following_id: int) -> None:
        with self._connect() as conn:
            row = conn.execute(f"{self._select_sql()} WHERE id = ?", (following_id,)).fetchone()
            if row is None:
                return
            record = self._record_from_row(row)
            latest_episode = max(0, int(record.latest_episode or 0))
            if latest_episode <= 0:
                conn.execute("UPDATE following SET homepage_prompt_pending = 0 WHERE id = ?", (following_id,))
                return
            latest_season = self._prompt_latest_season_number(conn, record)
            conn.execute(
                """
                UPDATE following
                SET homepage_prompt_pending = 0,
                    prompt_dismissed_latest_episode = ?,
                    prompt_dismissed_latest_season = ?
                WHERE id = ?
                """,
                (latest_episode, latest_season, following_id),
            )

    def _prompt_latest_season_number(self, conn, record: FollowingRecord) -> int:
        latest_episode = max(0, int(record.latest_episode or 0))
        latest_season = resolve_progress_season(
            record.season_number,
            latest_episode,
            fallback_season=record.season_number,
        )
        row = conn.execute(
            """
            SELECT seasons_json, episodes_json
            FROM following_detail_snapshots
            WHERE following_id = ?
            """,
            (record.id,),
        ).fetchone()
        if row is None:
            return latest_season
        snapshot_seasons = [
            season.season_number
            for season in (_season_from_dict(item) for item in _json_loads(row[0], []))
            if season.season_number > 0
        ]
        snapshot_seasons.extend(
            episode.season_number
            for episode in (_episode_from_dict(item) for item in _json_loads(row[1], []))
            if episode.season_number > 0 and not episode.is_special
        )
        if snapshot_seasons:
            return max(latest_season, max(snapshot_seasons))
        return latest_season

    def _record_params(self, record: FollowingRecord) -> tuple[object, ...]:
        return (
            record.title,
            record.original_title,
            record.media_kind,
            record.season_number,
            record.poster,
            record.backdrop,
            record.rating,
            record.provider,
            record.provider_id,
            _json_dumps(record.provider_priority),
            _json_dumps(record.external_ids),
            _json_dumps([_binding_to_dict(binding) for binding in record.source_bindings]),
            record.current_season_number,
            record.current_episode,
            record.position_seconds,
            1 if record.watched_latest_episode else 0,
            record.latest_episode,
            record.previous_latest_episode,
            record.total_episodes,
            1 if record.has_update else 0,
            record.new_episode_count,
            1 if record.homepage_prompt_pending else 0,
            record.prompt_snoozed_until,
            record.prompt_dismissed_latest_episode,
            self._resolved_prompt_dismissed_latest_season(record),
            record.created_at,
            record.updated_at,
            record.last_played_at,
            record.last_checked_at,
            record.next_check_after,
            record.last_error,
        )

    def _record_from_row(self, row) -> FollowingRecord:
        dismissed_latest_episode = int(row[24])
        dismissed_latest_season = int(row[25])
        return FollowingRecord(
            id=int(row[0]),
            title=str(row[1]),
            original_title=str(row[2]),
            media_kind=str(row[3]),
            season_number=int(row[4]),
            poster=str(row[5]),
            backdrop=str(row[6]),
            rating=str(row[7]),
            provider=str(row[8]),
            provider_id=str(row[9]),
            provider_priority=[str(item) for item in _json_loads(row[10], [])],
            external_ids={str(key): str(value) for key, value in dict(_json_loads(row[11], {})).items()},
            source_bindings=[_binding_from_dict(item) for item in _json_loads(row[12], [])],
            current_season_number=int(row[13]),
            current_episode=int(row[14]),
            position_seconds=int(row[15]),
            watched_latest_episode=bool(row[16]),
            latest_episode=int(row[17]),
            previous_latest_episode=int(row[18]),
            total_episodes=int(row[19]),
            has_update=bool(row[20]),
            new_episode_count=int(row[21]),
            homepage_prompt_pending=bool(row[22]),
            prompt_snoozed_until=int(row[23]),
            prompt_dismissed_latest_episode=dismissed_latest_episode,
            prompt_dismissed_latest_season=dismissed_latest_season
            or (
                resolve_progress_season(0, dismissed_latest_episode, fallback_season=int(row[4]))
                if dismissed_latest_episode > 0
                else 0
            ),
            created_at=int(row[26]),
            updated_at=int(row[27]),
            last_played_at=int(row[28]),
            last_checked_at=int(row[29]),
            next_check_after=int(row[30]),
            last_error=str(row[31]),
        )

    @staticmethod
    def _resolved_prompt_dismissed_latest_season(record: FollowingRecord) -> int:
        dismissed_episode = max(0, int(record.prompt_dismissed_latest_episode or 0))
        if dismissed_episode <= 0:
            return max(0, int(record.prompt_dismissed_latest_season or 0))
        dismissed_season = max(0, int(record.prompt_dismissed_latest_season or 0))
        if dismissed_season > 0:
            return dismissed_season
        return resolve_progress_season(0, dismissed_episode, fallback_season=record.season_number)

    def _select_sql(self) -> str:
        return """
            SELECT id, title, original_title, media_kind, season_number, poster, backdrop,
                   rating, provider, provider_id, provider_priority_json, external_ids_json,
                   source_bindings_json, current_season_number, current_episode, position_seconds,
                   watched_latest_episode, latest_episode, previous_latest_episode, total_episodes,
                   has_update, new_episode_count, homepage_prompt_pending, prompt_snoozed_until,
                   prompt_dismissed_latest_episode, prompt_dismissed_latest_season,
                   created_at, updated_at, last_played_at, last_checked_at,
                   next_check_after, last_error
            FROM following
        """
