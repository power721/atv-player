from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from atv_player.models import SpiderPluginConfig, SpiderPluginLogEntry


def _require_lastrowid(cursor: sqlite3.Cursor) -> int:
    lastrowid = cursor.lastrowid
    if lastrowid is None:
        raise RuntimeError("插入插件记录后缺少 lastrowid")
    return int(lastrowid)


class SpiderPluginRepository:
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
                CREATE TABLE IF NOT EXISTS spider_plugins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    source_value TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL,
                    cached_file_path TEXT NOT NULL DEFAULT '',
                    last_loaded_at INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS spider_plugin_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plugin_id INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )

    def add_plugin(self, source_type: str, source_value: str, display_name: str) -> SpiderPluginConfig:
        with self._connect() as conn:
            next_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM spider_plugins"
            ).fetchone()[0]
            cursor = conn.execute(
                """
                INSERT INTO spider_plugins (
                    source_type, source_value, display_name, enabled, sort_order,
                    cached_file_path, last_loaded_at, last_error
                )
                VALUES (?, ?, ?, 1, ?, '', 0, '')
                """,
                (source_type, source_value, display_name, next_order),
            )
        return self.get_plugin(_require_lastrowid(cursor))

    def get_plugin(self, plugin_id: int) -> SpiderPluginConfig:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, source_type, source_value, display_name, enabled, sort_order,
                       cached_file_path, last_loaded_at, last_error
                FROM spider_plugins
                WHERE id = ?
                """,
                (plugin_id,),
            ).fetchone()
        assert row is not None
        values = list(row)
        values[4] = bool(values[4])
        return SpiderPluginConfig(*values)

    def list_plugins(self) -> list[SpiderPluginConfig]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source_type, source_value, display_name, enabled, sort_order,
                       cached_file_path, last_loaded_at, last_error
                FROM spider_plugins
                ORDER BY sort_order ASC, id ASC
                """
            ).fetchall()
        plugins: list[SpiderPluginConfig] = []
        for row in rows:
            values = list(row)
            values[4] = bool(values[4])
            plugins.append(SpiderPluginConfig(*values))
        return plugins

    def update_plugin(
        self,
        plugin_id: int,
        *,
        display_name: str,
        enabled: bool,
        cached_file_path: str,
        last_loaded_at: int,
        last_error: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE spider_plugins
                SET display_name = ?, enabled = ?, cached_file_path = ?,
                    last_loaded_at = ?, last_error = ?
                WHERE id = ?
                """,
                (display_name, int(enabled), cached_file_path, last_loaded_at, last_error, plugin_id),
            )

    def move_plugin(self, plugin_id: int, direction: int) -> None:
        plugins = self.list_plugins()
        index = next(i for i, item in enumerate(plugins) if item.id == plugin_id)
        target = index + direction
        if not (0 <= target < len(plugins)):
            return
        plugins[index], plugins[target] = plugins[target], plugins[index]
        with self._connect() as conn:
            for order, item in enumerate(plugins):
                conn.execute(
                    "UPDATE spider_plugins SET sort_order = ? WHERE id = ?",
                    (order, item.id),
                )

    def delete_plugin(self, plugin_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM spider_plugin_logs WHERE plugin_id = ?", (plugin_id,))
            conn.execute("DELETE FROM spider_plugins WHERE id = ?", (plugin_id,))

    def append_log(self, plugin_id: int, level: str, message: str, created_at: int | None = None) -> None:
        timestamp = int(time.time()) if created_at is None else created_at
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO spider_plugin_logs (plugin_id, level, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (plugin_id, level, message, timestamp),
            )

    def list_logs(self, plugin_id: int) -> list[SpiderPluginLogEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, plugin_id, level, message, created_at
                FROM spider_plugin_logs
                WHERE plugin_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (plugin_id,),
            ).fetchall()
        return [SpiderPluginLogEntry(*row) for row in rows]
