import sqlite3
from pathlib import Path

from atv_player.models import AppConfig
from atv_player.plugins.repository import SpiderPluginRepository
from atv_player.storage import SettingsRepository


def test_local_playback_history_repository_round_trip_emby_source_metadata(tmp_path: Path) -> None:
    from atv_player.local_playback_history import LocalPlaybackHistoryRepository

    repo = LocalPlaybackHistoryRepository(tmp_path / "app.db")
    repo.save_history(
        "emby",
        "emby-1",
        {
            "vodName": "Emby Movie",
            "vodPic": "poster",
            "vodRemarks": "Episode 2",
            "episode": 1,
            "episodeUrl": "2.m3u8",
            "position": 45000,
            "opening": 0,
            "ending": 0,
            "speed": 1.25,
            "playlistIndex": 1,
            "createTime": 1713206400000,
        },
        source_name="Emby",
    )

    history = repo.get_history("emby", "emby-1")

    assert history is not None
    assert history.source_kind == "emby"
    assert history.source_key == ""
    assert history.source_name == "Emby"


def test_local_playback_history_repository_lists_and_deletes_jellyfin_records(tmp_path: Path) -> None:
    from atv_player.local_playback_history import LocalPlaybackHistoryRepository

    repo = LocalPlaybackHistoryRepository(tmp_path / "app.db")
    repo.save_history(
        "jellyfin",
        "jf-1",
        {
            "vodName": "Jellyfin Movie",
            "vodPic": "poster",
            "vodRemarks": "Episode 1",
            "episode": 0,
            "episodeUrl": "1.m3u8",
            "position": 10000,
            "opening": 0,
            "ending": 0,
            "speed": 1.0,
            "playlistIndex": 0,
            "createTime": 1713206400001,
        },
        source_name="Jellyfin",
    )

    records = repo.list_histories()
    repo.delete_history("jellyfin", "jf-1")

    assert [record.source_kind for record in records] == ["jellyfin"]
    assert repo.get_history("jellyfin", "jf-1") is None


def test_local_playback_history_repository_round_trip_feiniu_source_metadata(tmp_path: Path) -> None:
    from atv_player.local_playback_history import LocalPlaybackHistoryRepository

    repo = LocalPlaybackHistoryRepository(tmp_path / "app.db")
    repo.save_history(
        "feiniu",
        "fn-1",
        {
            "vodName": "Feiniu Movie",
            "vodPic": "poster",
            "vodRemarks": "Episode 2",
            "episode": 1,
            "episodeUrl": "2.m3u8",
            "position": 45000,
            "opening": 0,
            "ending": 0,
            "speed": 1.25,
            "playlistIndex": 1,
            "createTime": 1713206400000,
        },
        source_name="飞牛影视",
    )

    history = repo.get_history("feiniu", "fn-1")

    assert history is not None
    assert history.source_kind == "feiniu"
    assert history.source_name == "飞牛影视"


def test_local_playback_history_repository_migrates_spider_plugin_legacy_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE spider_plugins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_value TEXT NOT NULL,
                display_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL,
                cached_file_path TEXT NOT NULL DEFAULT '',
                last_loaded_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                config_text TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE spider_plugin_playback_history (
                plugin_id INTEGER NOT NULL,
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
                PRIMARY KEY (plugin_id, vod_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO spider_plugins (
                id, source_type, source_value, display_name, enabled, sort_order,
                cached_file_path, last_loaded_at, last_error, config_text
            )
            VALUES (1, 'local', '/plugins/demo.py', '红果短剧', 1, 0, '', 0, '', '')
            """
        )
        conn.execute(
            """
            INSERT INTO spider_plugin_playback_history (
                plugin_id, vod_id, vod_name, vod_pic, vod_remarks, episode,
                episode_url, position, opening, ending, speed, playlist_index, updated_at
            )
            VALUES (1, 'detail-1', '红果短剧', 'poster', '第2集', 1, '2.m3u8', 45000, 0, 0, 1.0, 0, 1713206400000)
            """
        )

    from atv_player.local_playback_history import LocalPlaybackHistoryRepository

    repo = LocalPlaybackHistoryRepository(db_path)
    records = repo.list_histories()

    assert len(records) == 1
    assert records[0].source_kind == "spider_plugin"
    assert records[0].source_key == "1"
    assert records[0].source_name == "红果短剧"
    assert records[0].key == "detail-1"


def test_local_playback_history_repository_reads_legacy_spider_plugin_rows_without_source_key(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE media_playback_history (
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
        conn.execute(
            """
            INSERT INTO media_playback_history (
                source_kind, source_key, source_name, vod_id, vod_name, vod_pic,
                vod_remarks, episode, episode_url, position, opening, ending,
                speed, playlist_index, updated_at
            )
            VALUES ('spider_plugin', '', '红果短剧', 'detail-1', '红果短剧', 'poster', '第2集', 1, '2.m3u8', 45000, 0, 0, 1.0, 0, 1713206400000)
            """
        )

    from atv_player.local_playback_history import LocalPlaybackHistoryRepository

    repo = LocalPlaybackHistoryRepository(db_path)
    history = repo.get_history("spider_plugin", "detail-1", source_key="7")

    assert history is not None
    assert history.key == "detail-1"
    assert history.source_key == ""
    assert history.episode == 1


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


def test_settings_repository_round_trip_persists_preferred_parse_key(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        preferred_parse_key="jx2",
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.preferred_parse_key == "jx2"
    assert saved == config


def test_settings_repository_round_trip_persists_preferred_danmaku_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        preferred_danmaku_enabled=False,
        preferred_danmaku_line_count=4,
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.preferred_danmaku_enabled is False
    assert saved.preferred_danmaku_line_count == 4
    assert saved == config


def test_settings_repository_migrates_missing_preferred_parse_key_column(tmp_path: Path) -> None:
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
                last_playback_source TEXT NOT NULL DEFAULT 'browse',
                last_playback_source_key TEXT NOT NULL DEFAULT '',
                last_playback_mode TEXT NOT NULL DEFAULT '',
                last_playback_path TEXT NOT NULL DEFAULT '',
                last_playback_vod_id TEXT NOT NULL DEFAULT '',
                last_playback_clicked_vod_id TEXT NOT NULL DEFAULT '',
                last_player_paused INTEGER NOT NULL DEFAULT 0,
                player_volume INTEGER NOT NULL DEFAULT 100,
                player_muted INTEGER NOT NULL DEFAULT 0,
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
                id, base_url, username, token, vod_token, last_path,
                last_active_window, last_playback_source, last_playback_source_key,
                last_playback_mode, last_playback_path, last_playback_vod_id,
                last_playback_clicked_vod_id, last_player_paused, player_volume,
                player_muted, main_window_geometry, player_window_geometry,
                player_main_splitter_state, browse_content_splitter_state
            )
            VALUES (1, 'http://127.0.0.1:4567', 'alice', '', '', '/', 'main', 'browse', '', '', '', '', '', 0, 100, 0, NULL, NULL, NULL, NULL)
            """
        )

    repo = SettingsRepository(db_path)

    assert repo.load_config().preferred_parse_key == ""


def test_settings_repository_migrates_missing_preferred_danmaku_columns(tmp_path: Path) -> None:
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
                last_playback_source TEXT NOT NULL DEFAULT 'browse',
                last_playback_source_key TEXT NOT NULL DEFAULT '',
                last_playback_mode TEXT NOT NULL DEFAULT '',
                last_playback_path TEXT NOT NULL DEFAULT '',
                last_playback_vod_id TEXT NOT NULL DEFAULT '',
                last_playback_clicked_vod_id TEXT NOT NULL DEFAULT '',
                last_player_paused INTEGER NOT NULL DEFAULT 0,
                player_volume INTEGER NOT NULL DEFAULT 100,
                player_muted INTEGER NOT NULL DEFAULT 0,
                preferred_parse_key TEXT NOT NULL DEFAULT '',
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
                id, base_url, username, token, vod_token, last_path,
                last_active_window, last_playback_source, last_playback_source_key,
                last_playback_mode, last_playback_path, last_playback_vod_id,
                last_playback_clicked_vod_id, last_player_paused, player_volume,
                player_muted, preferred_parse_key, main_window_geometry,
                player_window_geometry, player_main_splitter_state, browse_content_splitter_state
            )
            VALUES (1, 'http://127.0.0.1:4567', 'alice', '', '', '/', 'main', 'browse', '', '', '', '', '', 0, 100, 0, '', NULL, NULL, NULL, NULL)
            """
        )

    repo = SettingsRepository(db_path)
    saved = repo.load_config()

    assert saved.preferred_danmaku_enabled is True
    assert saved.preferred_danmaku_line_count == 1


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

    assert local_plugin.config_text == ""
    assert remote_plugin.config_text == ""

    repo.update_plugin(
        local_plugin.id,
        display_name="红果短剧本地",
        enabled=False,
        cached_file_path="",
        last_loaded_at=1713206400,
        last_error="缺少依赖: pyquery",
        config_text="site=https://example.com\ncookie=abc",
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
    assert plugins[1].config_text == "site=https://example.com\ncookie=abc"
    assert logs[0].message == "缺少依赖: pyquery"

    repo.delete_plugin(remote_plugin.id)

    assert [item.display_name for item in repo.list_plugins()] == ["红果短剧本地"]


def test_spider_plugin_repository_round_trip_playback_history(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SpiderPluginRepository(db_path)
    plugin = repo.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")

    repo.save_playback_history(
        plugin.id,
        "detail-1",
        {
            "vodName": "红果短剧",
            "vodPic": "poster-1",
            "vodRemarks": "第2集",
            "episode": 1,
            "episodeUrl": "https://media.example/2.m3u8",
            "position": 45000,
            "opening": 5000,
            "ending": 10000,
            "speed": 1.25,
            "playlistIndex": 1,
            "createTime": 1713206400000,
        },
    )

    history = repo.get_playback_history(plugin.id, "detail-1")

    assert history is not None
    assert history.key == "detail-1"
    assert history.vod_name == "红果短剧"
    assert history.episode == 1
    assert history.position == 45000
    assert history.speed == 1.25
    assert history.playlist_index == 1


def test_spider_plugin_repository_updates_existing_playback_history_and_deletes_with_plugin(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "app.db"
    repo = SpiderPluginRepository(db_path)
    plugin = repo.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")

    repo.save_playback_history(
        plugin.id,
        "detail-1",
        {
            "vodName": "旧标题",
            "vodPic": "poster-old",
            "vodRemarks": "第1集",
            "episode": 0,
            "episodeUrl": "https://media.example/1.m3u8",
            "position": 15000,
            "opening": 0,
            "ending": 0,
            "speed": 1.0,
            "playlistIndex": 0,
            "createTime": 1713206400000,
        },
    )
    repo.save_playback_history(
        plugin.id,
        "detail-1",
        {
            "vodName": "新标题",
            "vodPic": "poster-new",
            "vodRemarks": "第3集",
            "episode": 2,
            "episodeUrl": "https://media.example/3.m3u8",
            "position": 90000,
            "opening": 8000,
            "ending": 16000,
            "speed": 1.5,
            "playlistIndex": 1,
            "createTime": 1713206500000,
        },
    )

    updated = repo.get_playback_history(plugin.id, "detail-1")

    assert updated is not None
    assert updated.vod_name == "新标题"
    assert updated.episode == 2
    assert updated.position == 90000
    assert updated.speed == 1.5
    assert updated.playlist_index == 1

    repo.delete_plugin(plugin.id)

    assert repo.get_playback_history(plugin.id, "detail-1") is None


def test_spider_plugin_repository_lists_playback_histories_with_plugin_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SpiderPluginRepository(db_path)
    plugin = repo.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")

    repo.save_playback_history(
        plugin.id,
        "detail-1",
        {
            "vodName": "红果短剧",
            "vodPic": "poster-1",
            "vodRemarks": "第2集",
            "episode": 1,
            "episodeUrl": "https://media.example/2.m3u8",
            "position": 45000,
            "opening": 5000,
            "ending": 10000,
            "speed": 1.25,
            "playlistIndex": 1,
            "createTime": 1713206400000,
        },
    )

    records = repo.list_playback_histories()

    assert len(records) == 1
    assert records[0].key == "detail-1"
    assert records[0].source_kind == "spider_plugin"
    assert records[0].source_plugin_id == plugin.id
    assert records[0].source_plugin_name == "红果短剧"


def test_spider_plugin_repository_deletes_single_playback_history(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SpiderPluginRepository(db_path)
    plugin = repo.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")

    repo.save_playback_history(
        plugin.id,
        "detail-1",
        {
            "vodName": "红果短剧",
            "vodPic": "poster-1",
            "vodRemarks": "第1集",
            "episode": 0,
            "episodeUrl": "https://media.example/1.m3u8",
            "position": 15000,
            "opening": 0,
            "ending": 0,
            "speed": 1.0,
            "playlistIndex": 0,
            "createTime": 1713206400000,
        },
    )

    repo.delete_playback_history(plugin.id, "detail-1")

    assert repo.get_playback_history(plugin.id, "detail-1") is None


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


def test_spider_plugin_repository_migrates_missing_playlist_index_column(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE spider_plugin_playback_history (
                plugin_id INTEGER NOT NULL,
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
                updated_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (plugin_id, vod_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO spider_plugin_playback_history (
                plugin_id, vod_id, vod_name, vod_pic, vod_remarks,
                episode, episode_url, position, opening, ending, speed, updated_at
            )
            VALUES (1, 'detail-1', '红果短剧', 'poster', '第1集', 0, 'https://media.example/1.m3u8', 45000, 0, 0, 1.0, 1713206400000)
            """
        )

    repo = SpiderPluginRepository(db_path)
    history = repo.get_playback_history(1, "detail-1")

    assert history is not None
    assert history.playlist_index == 0


def test_spider_plugin_repository_migrates_missing_config_text_column(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE spider_plugins (
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
            INSERT INTO spider_plugins (
                source_type, source_value, display_name, enabled, sort_order,
                cached_file_path, last_loaded_at, last_error
            )
            VALUES ('local', '/plugins/红果短剧.py', '红果短剧', 1, 0, '', 0, '')
            """
        )

    repo = SpiderPluginRepository(db_path)
    plugin = repo.get_plugin(1)

    assert plugin.display_name == "红果短剧"
    assert plugin.config_text == ""
    repo.update_plugin(
        plugin.id,
        display_name=plugin.display_name,
        enabled=plugin.enabled,
        cached_file_path=plugin.cached_file_path,
        last_loaded_at=plugin.last_loaded_at,
        last_error=plugin.last_error,
        config_text="token=updated",
    )

    assert repo.get_plugin(1).config_text == "token=updated"
