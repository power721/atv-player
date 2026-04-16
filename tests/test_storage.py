import sqlite3
from pathlib import Path

from atv_player.models import AppConfig, SpiderPluginConfig
from atv_player.plugins.repository import SpiderPluginRepository
from atv_player.storage import SettingsRepository


def test_settings_repository_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        last_path="/Movies",
        last_active_window="player",
        last_playback_mode="folder",
        last_playback_path="/Movies",
        last_playback_vod_id="vod-1",
        last_playback_clicked_vod_id="vod-2",
        last_player_paused=True,
        player_volume=35,
        player_muted=True,
        main_window_geometry=None,
        player_window_geometry=None,
        player_main_splitter_state=b"split-main",
        browse_content_splitter_state=b"split-browse",
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved == config


def test_settings_repository_migrates_missing_last_player_paused_column(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE app_config (
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
                main_window_geometry BLOB,
                player_window_geometry BLOB,
                player_main_splitter_state BLOB
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
                vod_token,
                last_path,
                last_active_window,
                last_playback_mode,
                last_playback_path,
                last_playback_vod_id,
                last_playback_clicked_vod_id,
                main_window_geometry,
                player_window_geometry,
                player_main_splitter_state
            )
            VALUES (1, 'http://127.0.0.1:4567', 'alice', '', '', '/TV', 'player', 'detail', '/TV', 'vod-1', 'vod-1', NULL, NULL, NULL)
            """
        )

    repo = SettingsRepository(db_path)
    saved = repo.load_config()

    assert saved.last_player_paused is False


def test_settings_repository_migrates_missing_player_volume_column(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE app_config (
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
                player_main_splitter_state BLOB,
                browse_content_splitter_state BLOB
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
                player_main_splitter_state,
                browse_content_splitter_state
            )
            VALUES (1, 'http://127.0.0.1:4567', 'alice', '', '', '/TV', 'player', 'detail', '/TV', 'vod-1', 'vod-1', 0, NULL, NULL, NULL, NULL)
            """
        )

    repo = SettingsRepository(db_path)
    saved = repo.load_config()

    assert saved.player_volume == 100


def test_settings_repository_migrates_missing_player_muted_column(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE app_config (
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
                player_volume INTEGER NOT NULL DEFAULT 100,
                main_window_geometry BLOB,
                player_window_geometry BLOB,
                player_main_splitter_state BLOB,
                browse_content_splitter_state BLOB
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
                vod_token,
                last_path,
                last_active_window,
                last_playback_mode,
                last_playback_path,
                last_playback_vod_id,
                last_playback_clicked_vod_id,
                last_player_paused,
                player_volume,
                main_window_geometry,
                player_window_geometry,
                player_main_splitter_state,
                browse_content_splitter_state
            )
            VALUES (1, 'http://127.0.0.1:4567', 'alice', '', '', '/TV', 'player', 'detail', '/TV', 'vod-1', 'vod-1', 0, 100, NULL, NULL, NULL, NULL)
            """
        )

    repo = SettingsRepository(db_path)
    saved = repo.load_config()

    assert saved.player_muted is False


def test_settings_repository_clear_token_preserves_other_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)
    repo.save_config(
        AppConfig(
            base_url="http://127.0.0.1:4567",
            username="alice",
            token="token-123",
            vod_token="vod-123",
            last_path="/TV",
            last_active_window="player",
            last_playback_mode="detail",
            last_playback_path="/TV",
            last_playback_vod_id="vod-1",
            last_playback_clicked_vod_id="vod-1",
            last_player_paused=True,
            player_volume=35,
            player_muted=True,
            main_window_geometry=None,
            player_window_geometry=None,
            player_main_splitter_state=b"split-main",
            browse_content_splitter_state=b"split-browse",
        )
    )

    repo.clear_token()
    saved = repo.load_config()

    assert saved.base_url == "http://127.0.0.1:4567"
    assert saved.username == "alice"
    assert saved.token == ""
    assert saved.vod_token == ""
    assert saved.last_path == "/TV"
    assert saved.last_active_window == "player"
    assert saved.last_player_paused is True
    assert saved.player_volume == 35
    assert saved.player_muted is True
    assert saved.player_main_splitter_state == b"split-main"
    assert saved.browse_content_splitter_state == b"split-browse"


def test_spider_plugin_repository_round_trip_and_logs(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SpiderPluginRepository(db_path)

    local_plugin = repo.add_plugin(
        source_type="local",
        source_value="/plugins/红果短剧.py",
        display_name="红果短剧",
    )
    remote_plugin = repo.add_plugin(
        source_type="remote",
        source_value="https://example.com/spiders/hg.py",
        display_name="红果短剧远程",
    )

    repo.update_plugin(
        local_plugin.id,
        display_name="红果短剧本地",
        enabled=False,
        cached_file_path="",
        last_loaded_at=1713206400,
        last_error="缺少依赖: pyquery",
    )
    repo.append_log(local_plugin.id, "error", "缺少依赖: pyquery", created_at=1713206401)
    repo.move_plugin(remote_plugin.id, direction=-1)

    plugins = repo.list_plugins()
    logs = repo.list_logs(local_plugin.id)

    assert [(item.display_name, item.sort_order, item.enabled) for item in plugins] == [
        ("红果短剧远程", 0, True),
        ("红果短剧本地", 1, False),
    ]
    assert plugins[1].last_error == "缺少依赖: pyquery"
    assert logs[0].message == "缺少依赖: pyquery"

    repo.delete_plugin(remote_plugin.id)

    assert [item.display_name for item in repo.list_plugins()] == ["红果短剧本地"]


def test_spider_plugin_repository_migrates_tables_into_existing_settings_db(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE app_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                base_url TEXT NOT NULL,
                username TEXT NOT NULL,
                token TEXT NOT NULL,
                vod_token TEXT NOT NULL,
                last_path TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO app_config (id, base_url, username, token, vod_token, last_path)
            VALUES (1, 'http://127.0.0.1:4567', '', '', '', '/')
            """
        )

    repo = SpiderPluginRepository(db_path)
    created = repo.add_plugin(
        source_type="local",
        source_value="/plugins/红果短剧.py",
        display_name="红果短剧",
    )

    assert created.id > 0
    assert repo.list_plugins()[0].source_value == "/plugins/红果短剧.py"
