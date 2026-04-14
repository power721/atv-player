import sqlite3
from pathlib import Path

from atv_player.models import AppConfig


class SettingsRepository:
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
                CREATE TABLE IF NOT EXISTS app_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    base_url TEXT NOT NULL,
                    username TEXT NOT NULL,
                    token TEXT NOT NULL,
                    vod_token TEXT NOT NULL,
                    last_path TEXT NOT NULL,
                    last_active_window TEXT NOT NULL DEFAULT 'main',
                    last_playback_mode TEXT NOT NULL DEFAULT '',
                    last_playback_path TEXT NOT NULL DEFAULT '',
                    last_playback_vod_id TEXT NOT NULL DEFAULT '',
                    last_playback_clicked_vod_id TEXT NOT NULL DEFAULT '',
                    last_player_paused INTEGER NOT NULL DEFAULT 0,
                    main_window_geometry BLOB,
                    player_window_geometry BLOB,
                    player_main_splitter_state BLOB
                )
                """
            )
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(app_config)").fetchall()
            }
            if "vod_token" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN vod_token TEXT NOT NULL DEFAULT ''"
                )
            if "last_active_window" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_active_window TEXT NOT NULL DEFAULT 'main'"
                )
            if "last_playback_mode" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_playback_mode TEXT NOT NULL DEFAULT ''"
                )
            if "last_playback_path" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_playback_path TEXT NOT NULL DEFAULT ''"
                )
            if "last_playback_vod_id" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_playback_vod_id TEXT NOT NULL DEFAULT ''"
                )
            if "last_playback_clicked_vod_id" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_playback_clicked_vod_id TEXT NOT NULL DEFAULT ''"
                )
            if "last_player_paused" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN last_player_paused INTEGER NOT NULL DEFAULT 0"
                )
            if "player_main_splitter_state" not in columns:
                conn.execute(
                    "ALTER TABLE app_config ADD COLUMN player_main_splitter_state BLOB"
                )
            conn.execute(
                """
                INSERT INTO app_config (
                    id,
                    base_url,
                    username,
                    token,
                    vod_token,
                    last_path,
                    last_active_window,
                    last_playback_mode,
                    last_playback_path,
                    last_playback_vod_id,
                    last_playback_clicked_vod_id,
                    last_player_paused,
                    main_window_geometry,
                    player_window_geometry,
                    player_main_splitter_state
                )
                VALUES (1, 'http://127.0.0.1:4567', '', '', '', '/', 'main', '', '', '', '', 0, NULL, NULL, NULL)
                ON CONFLICT(id) DO NOTHING
                """
            )

    def load_config(self) -> AppConfig:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    base_url,
                    username,
                    token,
                    vod_token,
                    last_path,
                    last_active_window,
                    last_playback_mode,
                    last_playback_path,
                    last_playback_vod_id,
                    last_playback_clicked_vod_id,
                    last_player_paused,
                    main_window_geometry,
                    player_window_geometry,
                    player_main_splitter_state
                FROM app_config
                WHERE id = 1
                """
            ).fetchone()
        assert row is not None
        values = list(row)
        values[10] = bool(values[10])
        return AppConfig(*values)

    def save_config(self, config: AppConfig) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE app_config
                SET
                    base_url = ?,
                    username = ?,
                    token = ?,
                    vod_token = ?,
                    last_path = ?,
                    last_active_window = ?,
                    last_playback_mode = ?,
                    last_playback_path = ?,
                    last_playback_vod_id = ?,
                    last_playback_clicked_vod_id = ?,
                    last_player_paused = ?,
                    main_window_geometry = ?,
                    player_window_geometry = ?,
                    player_main_splitter_state = ?
                WHERE id = 1
                """,
                (
                    config.base_url,
                    config.username,
                    config.token,
                    config.vod_token,
                    config.last_path,
                    config.last_active_window,
                    config.last_playback_mode,
                    config.last_playback_path,
                    config.last_playback_vod_id,
                    config.last_playback_clicked_vod_id,
                    int(config.last_player_paused),
                    config.main_window_geometry,
                    config.player_window_geometry,
                    config.player_main_splitter_state,
                ),
            )

    def clear_token(self) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE app_config SET token = '', vod_token = '' WHERE id = 1")
