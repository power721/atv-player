from __future__ import annotations

import sqlite3
from pathlib import Path

from atv_player.models import LiveSourceConfig, LiveSourceEntry

_DEFAULT_SOURCE_NAME = "IPTV"
_DEFAULT_SOURCE_URL = "https://gh.llkk.cc/https://raw.githubusercontent.com/Kimentanm/aptv/master/m3u/iptv.m3u"


class LiveSourceRepository:
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
                CREATE TABLE IF NOT EXISTS live_source (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    source_value TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    last_refreshed_at INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    cache_text TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS live_source_entry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '',
                    channel_name TEXT NOT NULL,
                    stream_url TEXT NOT NULL,
                    sort_order INTEGER NOT NULL
                )
                """
            )
            existing = conn.execute("SELECT COUNT(*) FROM live_source WHERE is_default = 1").fetchone()[0]
            if existing == 0:
                next_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM live_source").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO live_source (
                        source_type, source_value, display_name, enabled, sort_order,
                        is_default, last_refreshed_at, last_error, cache_text
                    )
                    VALUES ('remote', ?, ?, 1, ?, 1, 0, '', '')
                    """,
                    (_DEFAULT_SOURCE_URL, _DEFAULT_SOURCE_NAME, next_order),
                )

    def add_source(self, source_type: str, source_value: str, display_name: str) -> LiveSourceConfig:
        with self._connect() as conn:
            next_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM live_source").fetchone()[0]
            cursor = conn.execute(
                """
                INSERT INTO live_source (
                    source_type, source_value, display_name, enabled, sort_order,
                    is_default, last_refreshed_at, last_error, cache_text
                )
                VALUES (?, ?, ?, 1, ?, 0, 0, '', '')
                """,
                (source_type, source_value, display_name, next_order),
            )
        return self.get_source(int(cursor.lastrowid))

    def get_source(self, source_id: int) -> LiveSourceConfig:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, source_type, source_value, display_name, enabled, sort_order,
                       is_default, last_refreshed_at, last_error, cache_text
                FROM live_source
                WHERE id = ?
                """,
                (source_id,),
            ).fetchone()
        assert row is not None
        values = list(row)
        values[4] = bool(values[4])
        values[6] = bool(values[6])
        return LiveSourceConfig(*values)

    def list_sources(self) -> list[LiveSourceConfig]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source_type, source_value, display_name, enabled, sort_order,
                       is_default, last_refreshed_at, last_error, cache_text
                FROM live_source
                ORDER BY sort_order ASC, id ASC
                """
            ).fetchall()
        result: list[LiveSourceConfig] = []
        for row in rows:
            values = list(row)
            values[4] = bool(values[4])
            values[6] = bool(values[6])
            result.append(LiveSourceConfig(*values))
        return result

    def update_source(
        self,
        source_id: int,
        *,
        display_name: str,
        enabled: bool,
        source_value: str,
        cache_text: str,
        last_error: str,
        last_refreshed_at: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE live_source
                SET display_name = ?, enabled = ?, source_value = ?, cache_text = ?,
                    last_error = ?, last_refreshed_at = ?
                WHERE id = ?
                """,
                (display_name, int(enabled), source_value, cache_text, last_error, last_refreshed_at, source_id),
            )

    def move_source(self, source_id: int, direction: int) -> None:
        sources = self.list_sources()
        index = next(i for i, item in enumerate(sources) if item.id == source_id)
        target = index + direction
        if not (0 <= target < len(sources)):
            return
        sources[index], sources[target] = sources[target], sources[index]
        with self._connect() as conn:
            for order, item in enumerate(sources):
                conn.execute("UPDATE live_source SET sort_order = ? WHERE id = ?", (order, item.id))

    def delete_source(self, source_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM live_source_entry WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM live_source WHERE id = ?", (source_id,))

    def add_manual_entry(self, source_id: int, *, group_name: str, channel_name: str, stream_url: str) -> LiveSourceEntry:
        with self._connect() as conn:
            next_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM live_source_entry WHERE source_id = ?",
                (source_id,),
            ).fetchone()[0]
            cursor = conn.execute(
                """
                INSERT INTO live_source_entry (source_id, group_name, channel_name, stream_url, sort_order)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_id, group_name, channel_name, stream_url, next_order),
            )
        return self.get_manual_entry(int(cursor.lastrowid))

    def get_manual_entry(self, entry_id: int) -> LiveSourceEntry:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, source_id, group_name, channel_name, stream_url, sort_order
                FROM live_source_entry
                WHERE id = ?
                """,
                (entry_id,),
            ).fetchone()
        assert row is not None
        return LiveSourceEntry(*row)

    def list_manual_entries(self, source_id: int) -> list[LiveSourceEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source_id, group_name, channel_name, stream_url, sort_order
                FROM live_source_entry
                WHERE source_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (source_id,),
            ).fetchall()
        return [LiveSourceEntry(*row) for row in rows]

    def move_manual_entry(self, entry_id: int, direction: int) -> None:
        entry = self.get_manual_entry(entry_id)
        entries = self.list_manual_entries(entry.source_id)
        index = next(i for i, item in enumerate(entries) if item.id == entry_id)
        target = index + direction
        if not (0 <= target < len(entries)):
            return
        entries[index], entries[target] = entries[target], entries[index]
        with self._connect() as conn:
            for order, item in enumerate(entries):
                conn.execute("UPDATE live_source_entry SET sort_order = ? WHERE id = ?", (order, item.id))
