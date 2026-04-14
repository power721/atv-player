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
                    last_path TEXT NOT NULL,
                    main_window_geometry BLOB,
                    player_window_geometry BLOB
                )
                """
            )
            conn.execute(
                """
                INSERT INTO app_config (
                    id,
                    base_url,
                    username,
                    token,
                    last_path,
                    main_window_geometry,
                    player_window_geometry
                )
                VALUES (1, 'http://127.0.0.1:4567', '', '', '/', NULL, NULL)
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
                    last_path,
                    main_window_geometry,
                    player_window_geometry
                FROM app_config
                WHERE id = 1
                """
            ).fetchone()
        assert row is not None
        return AppConfig(*row)

    def save_config(self, config: AppConfig) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE app_config
                SET
                    base_url = ?,
                    username = ?,
                    token = ?,
                    last_path = ?,
                    main_window_geometry = ?,
                    player_window_geometry = ?
                WHERE id = 1
                """,
                (
                    config.base_url,
                    config.username,
                    config.token,
                    config.last_path,
                    config.main_window_geometry,
                    config.player_window_geometry,
                ),
            )

    def clear_token(self) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE app_config SET token = '' WHERE id = 1")
