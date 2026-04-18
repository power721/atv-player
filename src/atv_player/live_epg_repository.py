from __future__ import annotations

import sqlite3
from pathlib import Path

from atv_player.models import LiveEpgConfig


class LiveEpgRepository:
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
                CREATE TABLE IF NOT EXISTS live_epg_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    epg_url TEXT NOT NULL DEFAULT '',
                    cache_text TEXT NOT NULL DEFAULT '',
                    last_refreshed_at INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                INSERT INTO live_epg_config (id, epg_url, cache_text, last_refreshed_at, last_error)
                VALUES (1, '', '', 0, '')
                ON CONFLICT(id) DO NOTHING
                """
            )

    def load(self) -> LiveEpgConfig:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, epg_url, cache_text, last_refreshed_at, last_error
                FROM live_epg_config
                WHERE id = 1
                """
            ).fetchone()
        assert row is not None
        return LiveEpgConfig(*row)

    def save_url(self, epg_url: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE live_epg_config SET epg_url = ? WHERE id = 1", (epg_url,))

    def save_refresh_result(self, *, cache_text: str, last_refreshed_at: int, last_error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE live_epg_config
                SET cache_text = ?, last_refreshed_at = ?, last_error = ?
                WHERE id = 1
                """,
                (cache_text, last_refreshed_at, last_error),
            )
