from __future__ import annotations

import sqlite3
from pathlib import Path

from atv_player.models import HistoryRecord


class LocalPlaybackHistoryRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS media_playback_history (
                    source_kind TEXT NOT NULL,
                    source_key TEXT NOT NULL DEFAULT '',
                    source_name TEXT NOT NULL DEFAULT '',
                    vod_id TEXT NOT NULL,
                    vod_name TEXT NOT NULL DEFAULT '',
                    vod_pic TEXT NOT NULL DEFAULT '',
                    vod_remarks TEXT NOT NULL DEFAULT '',
                    episode INTEGER NOT NULL DEFAULT 0,
                    episode_url TEXT NOT NULL DEFAULT '',
                    position INTEGER NOT NULL DEFAULT 0,
                    opening INTEGER NOT NULL DEFAULT 0,
                    ending INTEGER NOT NULL DEFAULT 0,
                    speed REAL NOT NULL DEFAULT 1.0,
                    playlist_index INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (source_kind, source_key, vod_id)
                )
                """
            )
            self._migrate_spider_plugin_history(conn)

    def _migrate_spider_plugin_history(self, conn: sqlite3.Connection) -> None:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        if "spider_plugin_playback_history" not in tables or "spider_plugins" not in tables:
            return
        rows = conn.execute(
            """
            SELECT history.plugin_id, history.vod_id, history.vod_name, history.vod_pic, history.vod_remarks,
                   history.episode, history.episode_url, history.position, history.opening,
                   history.ending, history.speed, history.playlist_index, history.updated_at,
                   plugin.display_name
            FROM spider_plugin_playback_history AS history
            JOIN spider_plugins AS plugin ON plugin.id = history.plugin_id
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO media_playback_history (
                    source_kind, source_key, source_name, vod_id, vod_name, vod_pic,
                    vod_remarks, episode, episode_url, position, opening, ending,
                    speed, playlist_index, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "spider_plugin",
                    str(row[0]),
                    str(row[13] or ""),
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    int(row[5]),
                    row[6],
                    int(row[7]),
                    int(row[8]),
                    int(row[9]),
                    float(row[10]),
                    int(row[11]),
                    int(row[12]),
                ),
            )

    def get_history(self, source_kind: str, vod_id: str, source_key: str = "") -> HistoryRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT source_kind, source_key, source_name, vod_id, vod_name, vod_pic, vod_remarks,
                       episode, episode_url, position, opening, ending, speed, playlist_index, updated_at
                FROM media_playback_history
                WHERE source_kind = ? AND source_key = ? AND vod_id = ?
                """,
                (source_kind, source_key, vod_id),
            ).fetchone()
            if row is None and source_kind == "spider_plugin" and source_key:
                row = conn.execute(
                    """
                    SELECT source_kind, source_key, source_name, vod_id, vod_name, vod_pic, vod_remarks,
                           episode, episode_url, position, opening, ending, speed, playlist_index, updated_at
                    FROM media_playback_history
                    WHERE source_kind = ? AND source_key = '' AND vod_id = ?
                    """,
                    (source_kind, vod_id),
                ).fetchone()
        if row is None:
            return None
        return HistoryRecord(
            id=0,
            key=row[3],
            vod_name=row[4],
            vod_pic=row[5],
            vod_remarks=row[6],
            episode=int(row[7]),
            episode_url=row[8],
            position=int(row[9]),
            opening=int(row[10]),
            ending=int(row[11]),
            speed=float(row[12]),
            create_time=int(row[14]),
            playlist_index=int(row[13]),
            source_kind=str(row[0]),
            source_key=str(row[1]),
            source_name=str(row[2]),
            source_plugin_id=int(row[1]) if str(row[0]) == "spider_plugin" and str(row[1]).isdigit() else 0,
            source_plugin_name=str(row[2]) if str(row[0]) == "spider_plugin" else "",
        )

    def save_history(
        self,
        source_kind: str,
        vod_id: str,
        payload: dict[str, object],
        *,
        source_key: str = "",
        source_name: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO media_playback_history (
                    source_kind, source_key, source_name, vod_id, vod_name, vod_pic, vod_remarks,
                    episode, episode_url, position, opening, ending, speed, playlist_index, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_kind, source_key, vod_id) DO UPDATE SET
                    source_name = excluded.source_name,
                    vod_name = excluded.vod_name,
                    vod_pic = excluded.vod_pic,
                    vod_remarks = excluded.vod_remarks,
                    episode = excluded.episode,
                    episode_url = excluded.episode_url,
                    position = excluded.position,
                    opening = excluded.opening,
                    ending = excluded.ending,
                    speed = excluded.speed,
                    playlist_index = excluded.playlist_index,
                    updated_at = excluded.updated_at
                """,
                (
                    source_kind,
                    source_key,
                    source_name,
                    vod_id,
                    str(payload.get("vodName", "")),
                    str(payload.get("vodPic", "")),
                    str(payload.get("vodRemarks", "")),
                    int(payload.get("episode", 0)),
                    str(payload.get("episodeUrl", "")),
                    int(payload.get("position", 0)),
                    int(payload.get("opening", 0)),
                    int(payload.get("ending", 0)),
                    float(payload.get("speed", 1.0)),
                    int(payload.get("playlistIndex", 0)),
                    int(payload.get("createTime", 0)),
                ),
            )

    def list_histories(self) -> list[HistoryRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_kind, source_key, source_name, vod_id, vod_name, vod_pic, vod_remarks,
                       episode, episode_url, position, opening, ending, speed, playlist_index, updated_at
                FROM media_playback_history
                """
            ).fetchall()
        return [
            HistoryRecord(
                id=0,
                key=row[3],
                vod_name=row[4],
                vod_pic=row[5],
                vod_remarks=row[6],
                episode=int(row[7]),
                episode_url=row[8],
                position=int(row[9]),
                opening=int(row[10]),
                ending=int(row[11]),
                speed=float(row[12]),
                create_time=int(row[14]),
                playlist_index=int(row[13]),
                source_kind=str(row[0]),
                source_key=str(row[1]),
                source_name=str(row[2]),
                source_plugin_id=int(row[1]) if str(row[0]) == "spider_plugin" and str(row[1]).isdigit() else 0,
                source_plugin_name=str(row[2]) if str(row[0]) == "spider_plugin" else "",
            )
            for row in rows
        ]

    def delete_history(self, source_kind: str, vod_id: str, source_key: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM media_playback_history WHERE source_kind = ? AND source_key = ? AND vod_id = ?",
                (source_kind, source_key, vod_id),
            )
