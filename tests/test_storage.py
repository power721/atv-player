import sqlite3
from pathlib import Path
import re

import pytest

from atv_player.models import AppConfig
from atv_player.plugins.repository import SpiderPluginRepository
from atv_player.storage import SettingsRepository


def test_settings_repository_creates_stable_app_identity(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")

    identity = repo.ensure_app_identity()
    repeated = repo.ensure_app_identity()
    reloaded = SettingsRepository(tmp_path / "app.db").ensure_app_identity()

    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.[0-9a-f]{16}",
        identity.installation_id,
    )
    assert identity.installation_id == repeated.installation_id == reloaded.installation_id
    assert identity.created_at > 0
    assert repeated.created_at == identity.created_at
    assert reloaded.created_at == identity.created_at


def test_settings_repository_preserves_existing_app_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO app_identity (id, installation_id, created_at)
            VALUES (1, 'existing-id.abcdef0123456789', 123)
            """
        )

    identity = repo.ensure_app_identity()

    assert identity.installation_id == "existing-id.abcdef0123456789"
    assert identity.created_at == 123


def test_settings_repository_app_identity_row_is_database_immutable(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)
    identity = repo.ensure_app_identity()

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                UPDATE app_identity
                SET installation_id = 'changed.abcdef0123456789'
                WHERE id = 1
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM app_identity WHERE id = 1")

    assert repo.ensure_app_identity() == identity


def test_settings_repository_round_trips_disabled_source_preferences(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()
    config.disabled_danmaku_provider_ids = ["youku", "mgtv", "migu"]
    config.disabled_metadata_provider_ids = ["tmdb", "official_douban", "migu"]

    repo.save_config(config)
    loaded = repo.load_config()

    assert loaded.disabled_danmaku_provider_ids == ["youku", "mgtv", "migu"]
    assert loaded.disabled_metadata_provider_ids == ["tmdb", "official_douban", "migu"]


def test_settings_repository_round_trips_builtin_tab_overrides(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()
    config.builtin_tab_overrides_json = '{"order":["history","douban"],"hidden":["telegram"],"renames":{"douban":"电影"}}'

    repo.save_config(config)
    loaded = repo.load_config()

    assert loaded.builtin_tab_overrides_json == (
        '{"order":["history","douban"],"hidden":["telegram"],"renames":{"douban":"电影"}}'
    )


def test_settings_repository_round_trips_telegram_api_config(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()
    config.telegram_api_id = 12345
    config.telegram_api_hash = " telegram-api-hash "

    repo.save_config(config)
    loaded = repo.load_config()

    assert loaded.telegram_api_id == 12345
    assert loaded.telegram_api_hash == "telegram-api-hash"


def test_settings_repository_round_trips_home_mode(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()
    assert config.home_mode == "browse"

    config.home_mode = "media"
    repo.save_config(config)
    loaded = repo.load_config()

    assert loaded.home_mode == "media"


def test_settings_repository_normalizes_invalid_home_mode(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()

    config.home_mode = "nonexistent"
    repo.save_config(config)
    loaded = repo.load_config()

    assert loaded.home_mode == "browse"


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


def test_local_playback_history_round_trip_persists_grouped_source_indexes(tmp_path: Path) -> None:
    from atv_player.local_playback_history import LocalPlaybackHistoryRepository

    repo = LocalPlaybackHistoryRepository(tmp_path / "app.db")
    repo.save_history(
        "spider_plugin",
        "detail-1",
        {
            "vodName": "红果短剧",
            "vodPic": "",
            "vodRemarks": "第2集",
            "episode": 1,
            "episodeUrl": "https://b2/2.m3u8",
            "position": 90000,
            "opening": 5000,
            "ending": 10000,
            "speed": 1.25,
            "playlistIndex": 3,
            "sourceGroupIndex": 1,
            "sourceIndex": 1,
            "createTime": 42,
        },
        source_key="7",
        source_name="红果短剧",
    )

    history = repo.get_history("spider_plugin", "detail-1", source_key="7")

    assert history is not None
    assert history.playlist_index == 3
    assert history.source_group_index == 1
    assert history.source_index == 1


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
        last_selected_tab="history",
        last_selected_category_tab="telegram",
        last_selected_category_id="movie",
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


def test_settings_repository_round_trip_persists_youtube_preferences(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        youtube_cookie_browser="edge",
        youtube_max_height=1440,
        youtube_video_codec="av1",
        youtube_default_subtitle_lang="zh-CN",
        youtube_default_audio_lang="zh",
        youtube_metadata_language="zh-HK",
        youtube_region="CN",
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.youtube_cookie_browser == "edge"
    assert saved.youtube_max_height == 1440
    assert saved.youtube_video_codec == "av1"
    assert saved.youtube_default_subtitle_lang == "zh-CN"
    assert saved.youtube_default_audio_lang == "zh"
    assert saved.youtube_metadata_language == "zh-HK"
    assert saved.youtube_region == "CN"
    assert saved == config


def test_settings_repository_round_trip_persists_global_search_history(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        global_search_history=["庆余年", "琅琊榜", "藏海传"],
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.global_search_history == ["庆余年", "琅琊榜", "藏海传"]
    assert saved == config


def test_settings_repository_round_trip_preserves_extended_global_search_history(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    history = [f"关键词{i}" for i in range(1, 13)]
    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        global_search_history=history,
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.global_search_history == history


def test_settings_repository_round_trip_persists_global_search_hot_source(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        global_search_hot_source="iqiyi",
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.global_search_hot_source == "iqiyi"
    assert saved == config


def test_settings_repository_defaults_theme_mode_to_system(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")

    assert repo.load_config().theme_mode == "system"


def test_settings_repository_round_trip_persists_theme_mode(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = AppConfig(theme_mode="dark")

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.theme_mode == "dark"


def test_settings_repository_normalizes_invalid_theme_mode(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    repo.save_config(AppConfig(theme_mode="sepia"))

    assert repo.load_config().theme_mode == "system"


def test_settings_repository_round_trip_persists_playback_settings(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        youtube_cookie_browser="edge",
        youtube_max_height=1080,
        mpv_render_profile="vulkan",
        mpv_cache_size_mb=768,
        mpv_hwdec_mode="no",
        mpv_network_timeout_seconds=25,
        mpv_default_readahead_secs=45,
        mpv_extra_options="demuxer-max-back-bytes=256M\ncache-pause-wait=8",
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.youtube_cookie_browser == "edge"
    assert saved.youtube_max_height == 1080
    assert saved.mpv_render_profile == "vulkan"
    assert saved.mpv_cache_size_mb == 768
    assert saved.mpv_hwdec_mode == "no"
    assert saved.mpv_network_timeout_seconds == 25
    assert saved.mpv_default_readahead_secs == 45
    assert saved.mpv_extra_options == "demuxer-max-back-bytes=256M\ncache-pause-wait=8"
    assert saved == config


def test_settings_repository_accepts_auto_copy_hwdec_mode(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    repo.save_config(AppConfig(mpv_hwdec_mode="auto-copy"))

    assert repo.load_config().mpv_hwdec_mode == "auto-copy"


def test_settings_repository_normalizes_invalid_mpv_render_profile(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    repo.save_config(AppConfig(mpv_render_profile="experimental"))

    assert repo.load_config().mpv_render_profile == "auto"


def test_settings_repository_maps_legacy_software_hwdec_to_render_profile(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)
    repo.save_config(AppConfig(mpv_hwdec_mode="no"))
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE app_config RENAME TO app_config_new")
        conn.execute(
            """
            CREATE TABLE app_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                base_url TEXT NOT NULL,
                username TEXT NOT NULL,
                token TEXT NOT NULL,
                vod_token TEXT NOT NULL,
                last_path TEXT NOT NULL,
                mpv_hwdec_mode TEXT NOT NULL DEFAULT 'auto-safe'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO app_config (
                id, base_url, username, token, vod_token, last_path, mpv_hwdec_mode
            )
            SELECT id, base_url, username, token, vod_token, last_path, mpv_hwdec_mode
            FROM app_config_new
            """
        )
        conn.execute("DROP TABLE app_config_new")

    assert SettingsRepository(db_path).load_config().mpv_render_profile == "software"


def test_settings_repository_round_trip_persists_playback_auto_switch_source_flag(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        playback_auto_switch_source_on_failure=True,
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.playback_auto_switch_source_on_failure is True
    assert saved == config


def test_settings_repository_defaults_m3u_proxy_segment_prefetch_size_to_two(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")

    assert repo.load_config().m3u_proxy_segment_prefetch_size == 2


def test_settings_repository_round_trip_persists_m3u_proxy_segment_prefetch_size(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = AppConfig(m3u_proxy_segment_prefetch_size=5)

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.m3u_proxy_segment_prefetch_size == 5
    assert saved == config


def test_settings_repository_migrates_missing_playback_settings_columns(tmp_path: Path) -> None:
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
                metadata_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                episode_title_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                metadata_douban_cookie TEXT NOT NULL DEFAULT '',
                metadata_tmdb_api_key TEXT NOT NULL DEFAULT '',
                metadata_bangumi_access_token TEXT NOT NULL DEFAULT '',
                last_path TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO app_config (id, base_url, username, token, vod_token, last_path) VALUES (1, 'http://127.0.0.1:4567', '', '', '', '/')"
        )

    config = SettingsRepository(db_path).load_config()

    assert config.youtube_cookie_browser == ""
    assert config.youtube_max_height == 1080
    assert config.mpv_render_profile == "auto"
    assert config.mpv_cache_size_mb == 512
    assert config.mpv_hwdec_mode == "auto-safe"
    assert config.mpv_network_timeout_seconds == 15
    assert config.mpv_default_readahead_secs == 20
    assert config.mpv_extra_options == ""


def test_settings_repository_normalizes_invalid_youtube_max_height_values(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    repo.save_config(AppConfig(youtube_max_height=999))

    assert repo.load_config().youtube_max_height == 1080


def test_settings_repository_normalizes_legacy_zero_youtube_max_height_to_1080(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    repo.save_config(AppConfig(youtube_max_height=0))

    assert repo.load_config().youtube_max_height == 1080


def test_settings_repository_defaults_and_normalizes_youtube_video_codec(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    assert repo.load_config().youtube_video_codec == "vp9"

    repo.save_config(AppConfig(youtube_video_codec="invalid"))

    assert repo.load_config().youtube_video_codec == "vp9"


def test_settings_repository_migrates_missing_m3u_proxy_segment_prefetch_size_column(tmp_path: Path) -> None:
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
                metadata_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                episode_title_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                metadata_douban_cookie TEXT NOT NULL DEFAULT '',
                metadata_tmdb_api_key TEXT NOT NULL DEFAULT '',
                metadata_bangumi_access_token TEXT NOT NULL DEFAULT '',
                last_path TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO app_config (id, base_url, username, token, vod_token, last_path) VALUES (1, 'http://127.0.0.1:4567', '', '', '', '/')"
        )

    config = SettingsRepository(db_path).load_config()

    assert config.m3u_proxy_segment_prefetch_size == 2


def test_settings_repository_migrates_missing_playback_auto_switch_source_column(tmp_path: Path) -> None:
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
                metadata_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                episode_title_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                metadata_douban_cookie TEXT NOT NULL DEFAULT '',
                metadata_tmdb_api_key TEXT NOT NULL DEFAULT '',
                metadata_bangumi_access_token TEXT NOT NULL DEFAULT '',
                last_path TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO app_config (id, base_url, username, token, vod_token, last_path) VALUES (1, 'http://127.0.0.1:4567', '', '', '', '/')"
        )

    config = SettingsRepository(db_path).load_config()

    assert config.playback_auto_switch_source_on_failure is False


def test_settings_repository_normalizes_invalid_m3u_proxy_segment_prefetch_size_values(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    repo.save_config(AppConfig(m3u_proxy_segment_prefetch_size=99))

    assert repo.load_config().m3u_proxy_segment_prefetch_size == 10


def test_settings_repository_round_trip_persists_metadata_credentials(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        metadata_douban_cookie="bid=demo; ll=118282",
        metadata_tmdb_api_key="tmdb-demo-key",
        metadata_tmdb_proxy_base_url="https://tmdb.example.com",
        metadata_bangumi_access_token="bgm-demo-token",
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.metadata_douban_cookie == "bid=demo; ll=118282"
    assert saved.metadata_tmdb_api_key == "tmdb-demo-key"
    assert saved.metadata_tmdb_proxy_base_url == "https://tmdb.example.com"
    assert saved.metadata_bangumi_access_token == "bgm-demo-token"
    assert saved == config


def test_settings_repository_round_trip_persists_metadata_enhancement_toggle(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        metadata_enhancement_enabled=False,
        metadata_douban_cookie="bid=demo; ll=118282",
        metadata_tmdb_api_key="tmdb-demo-key",
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.metadata_enhancement_enabled is False
    assert saved.metadata_douban_cookie == "bid=demo; ll=118282"
    assert saved.metadata_tmdb_api_key == "tmdb-demo-key"
    assert saved == config


def test_settings_repository_round_trip_persists_network_proxy_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        network_proxy_mode="socks5",
        network_proxy_url="socks5://user:pass@127.0.0.1:1080",
        network_proxy_bypass_rules=["localhost", "127.0.0.1", "10.0.0.0/8"],
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.network_proxy_mode == "socks5"
    assert saved.network_proxy_url == "socks5://user:pass@127.0.0.1:1080"
    assert saved.network_proxy_bypass_rules == ["localhost", "127.0.0.1", "10.0.0.0/8"]


def test_settings_repository_round_trip_persists_episode_title_enhancement_toggle(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        episode_title_enhancement_enabled=False,
        metadata_tmdb_api_key="tmdb-demo-key",
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.episode_title_enhancement_enabled is False
    assert saved.metadata_tmdb_api_key == "tmdb-demo-key"
    assert saved == config


def test_settings_repository_round_trip_persists_danmaku_readability_settings(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = AppConfig(
        preferred_danmaku_opacity=63,
        preferred_danmaku_outline_strength="off",
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.preferred_danmaku_opacity == 65
    assert saved.preferred_danmaku_outline_strength == "off"


def test_settings_repository_normalizes_invalid_danmaku_readability_settings(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")

    repo.save_config(
        AppConfig(
            preferred_danmaku_opacity=999,
            preferred_danmaku_outline_strength="neon",
        )
    )

    saved = repo.load_config()

    assert saved.preferred_danmaku_opacity == 100
    assert saved.preferred_danmaku_outline_strength == "strong"


def test_settings_repository_migrates_missing_metadata_credential_columns(tmp_path: Path) -> None:
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
                player_wide_mode INTEGER NOT NULL DEFAULT 0,
                player_log_visible INTEGER NOT NULL DEFAULT 1,
                preferred_parse_key TEXT NOT NULL DEFAULT '',
                preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_render_mode TEXT NOT NULL DEFAULT 'static',
                preferred_danmaku_color_mode TEXT NOT NULL DEFAULT 'source',
                preferred_danmaku_uniform_color TEXT NOT NULL DEFAULT '#FFFFFF',
                preferred_danmaku_position_preset TEXT NOT NULL DEFAULT 'top',
                preferred_danmaku_scroll_speed REAL NOT NULL DEFAULT 1.0,
                preferred_danmaku_font_size INTEGER NOT NULL DEFAULT 32,
                main_window_geometry BLOB,
                player_window_geometry BLOB,
                player_main_splitter_state BLOB,
                browse_content_splitter_state BLOB,
                last_selected_tab TEXT NOT NULL DEFAULT 'douban',
                last_selected_category_tab TEXT NOT NULL DEFAULT '',
                last_selected_category_id TEXT NOT NULL DEFAULT '',
                global_search_history TEXT NOT NULL DEFAULT '[]',
                global_search_hot_source TEXT NOT NULL DEFAULT '360'
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
                player_muted, player_wide_mode, player_log_visible, preferred_parse_key,
                preferred_danmaku_enabled, preferred_danmaku_line_count,
                preferred_danmaku_render_mode, preferred_danmaku_color_mode,
                preferred_danmaku_uniform_color, preferred_danmaku_position_preset,
                preferred_danmaku_scroll_speed, preferred_danmaku_font_size,
                main_window_geometry, player_window_geometry, player_main_splitter_state,
                browse_content_splitter_state, last_selected_tab, last_selected_category_tab,
                last_selected_category_id, global_search_history, global_search_hot_source
            )
            VALUES (
                1, 'http://127.0.0.1:4567', '', '', '', '/', 'main', 'browse', '', '', '', '', '',
                0, 100, 0, 0, 1, '', 1, 1, 'static', 'source', '#FFFFFF', 'top', 1.0, 32,
                NULL, NULL, NULL, NULL, 'douban', '', '', '[]', '360'
            )
            """
        )

    repo = SettingsRepository(db_path)
    config = repo.load_config()

    assert config.metadata_douban_cookie == ""
    assert config.metadata_tmdb_api_key == ""
    assert config.metadata_bangumi_access_token == ""


def test_settings_repository_migrates_missing_metadata_enhancement_column(tmp_path: Path) -> None:
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
                metadata_douban_cookie TEXT NOT NULL DEFAULT '',
                metadata_tmdb_api_key TEXT NOT NULL DEFAULT '',
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
                player_wide_mode INTEGER NOT NULL DEFAULT 0,
                player_log_visible INTEGER NOT NULL DEFAULT 1,
                preferred_parse_key TEXT NOT NULL DEFAULT '',
                preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_render_mode TEXT NOT NULL DEFAULT 'static',
                preferred_danmaku_color_mode TEXT NOT NULL DEFAULT 'source',
                preferred_danmaku_uniform_color TEXT NOT NULL DEFAULT '#FFFFFF',
                preferred_danmaku_position_preset TEXT NOT NULL DEFAULT 'top',
                preferred_danmaku_scroll_speed REAL NOT NULL DEFAULT 1.0,
                preferred_danmaku_font_size INTEGER NOT NULL DEFAULT 32,
                main_window_geometry BLOB,
                player_window_geometry BLOB,
                player_main_splitter_state BLOB,
                browse_content_splitter_state BLOB,
                last_selected_tab TEXT NOT NULL DEFAULT 'douban',
                last_selected_category_tab TEXT NOT NULL DEFAULT '',
                last_selected_category_id TEXT NOT NULL DEFAULT '',
                global_search_history TEXT NOT NULL DEFAULT '[]',
                global_search_hot_source TEXT NOT NULL DEFAULT '360'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO app_config (
                id, base_url, username, token, vod_token, metadata_douban_cookie,
                metadata_tmdb_api_key, last_path, last_active_window, last_playback_source,
                last_playback_source_key, last_playback_mode, last_playback_path,
                last_playback_vod_id, last_playback_clicked_vod_id, last_player_paused,
                player_volume, player_muted, player_wide_mode, player_log_visible,
                preferred_parse_key, preferred_danmaku_enabled, preferred_danmaku_line_count,
                preferred_danmaku_render_mode, preferred_danmaku_color_mode,
                preferred_danmaku_uniform_color, preferred_danmaku_position_preset,
                preferred_danmaku_scroll_speed, preferred_danmaku_font_size,
                main_window_geometry, player_window_geometry, player_main_splitter_state,
                browse_content_splitter_state, last_selected_tab, last_selected_category_tab,
                last_selected_category_id, global_search_history, global_search_hot_source
            )
            VALUES (
                1, 'http://127.0.0.1:4567', '', '', '', '', '', '/', 'main', 'browse', '', '', '', '', '',
                0, 100, 0, 0, 1, '', 1, 1, 'static', 'source', '#FFFFFF', 'top', 1.0, 32,
                NULL, NULL, NULL, NULL, 'douban', '', '', '[]', '360'
            )
            """
        )

    repo = SettingsRepository(db_path)
    config = repo.load_config()

    assert config.metadata_enhancement_enabled is True


def test_settings_repository_migrates_missing_episode_title_enhancement_column(tmp_path: Path) -> None:
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
                metadata_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                metadata_douban_cookie TEXT NOT NULL DEFAULT '',
                metadata_tmdb_api_key TEXT NOT NULL DEFAULT '',
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
                player_wide_mode INTEGER NOT NULL DEFAULT 0,
                player_log_visible INTEGER NOT NULL DEFAULT 1,
                preferred_parse_key TEXT NOT NULL DEFAULT '',
                preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_render_mode TEXT NOT NULL DEFAULT 'static',
                preferred_danmaku_color_mode TEXT NOT NULL DEFAULT 'source',
                preferred_danmaku_uniform_color TEXT NOT NULL DEFAULT '#FFFFFF',
                preferred_danmaku_position_preset TEXT NOT NULL DEFAULT 'top',
                preferred_danmaku_scroll_speed REAL NOT NULL DEFAULT 1.0,
                preferred_danmaku_font_size INTEGER NOT NULL DEFAULT 32,
                main_window_geometry BLOB,
                player_window_geometry BLOB,
                player_main_splitter_state BLOB,
                browse_content_splitter_state BLOB,
                last_selected_tab TEXT NOT NULL DEFAULT 'douban',
                last_selected_category_tab TEXT NOT NULL DEFAULT '',
                last_selected_category_id TEXT NOT NULL DEFAULT '',
                global_search_history TEXT NOT NULL DEFAULT '[]',
                global_search_hot_source TEXT NOT NULL DEFAULT '360'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO app_config (
                id, base_url, username, token, vod_token, metadata_enhancement_enabled,
                metadata_douban_cookie, metadata_tmdb_api_key, last_path, last_active_window,
                last_playback_source, last_playback_source_key, last_playback_mode, last_playback_path,
                last_playback_vod_id, last_playback_clicked_vod_id, last_player_paused,
                player_volume, player_muted, player_wide_mode, player_log_visible,
                preferred_parse_key, preferred_danmaku_enabled, preferred_danmaku_line_count,
                preferred_danmaku_render_mode, preferred_danmaku_color_mode,
                preferred_danmaku_uniform_color, preferred_danmaku_position_preset,
                preferred_danmaku_scroll_speed, preferred_danmaku_font_size,
                main_window_geometry, player_window_geometry, player_main_splitter_state,
                browse_content_splitter_state, last_selected_tab, last_selected_category_tab,
                last_selected_category_id, global_search_history, global_search_hot_source
            )
            VALUES (
                1, 'http://127.0.0.1:4567', '', '', '', 1, '', '', '/', 'main', 'browse', '', '', '', '', '',
                0, 100, 0, 0, 1, '', 1, 1, 'static', 'source', '#FFFFFF', 'top', 1.0, 32,
                NULL, NULL, NULL, NULL, 'douban', '', '', '[]', '360'
            )
            """
        )

    repo = SettingsRepository(db_path)
    config = repo.load_config()

    assert config.episode_title_enhancement_enabled is True


def test_settings_repository_migrates_missing_logging_enabled_column(tmp_path: Path) -> None:
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
                theme_mode TEXT NOT NULL DEFAULT 'system',
                metadata_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                episode_title_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                metadata_douban_cookie TEXT NOT NULL DEFAULT '',
                metadata_tmdb_api_key TEXT NOT NULL DEFAULT '',
                metadata_bangumi_access_token TEXT NOT NULL DEFAULT '',
                network_proxy_mode TEXT NOT NULL DEFAULT 'direct',
                network_proxy_url TEXT NOT NULL DEFAULT '',
                network_proxy_bypass_rules TEXT NOT NULL DEFAULT '[]',
                youtube_cookie_browser TEXT NOT NULL DEFAULT '',
                mpv_cache_size_mb INTEGER NOT NULL DEFAULT 512,
                mpv_hwdec_mode TEXT NOT NULL DEFAULT 'auto-safe',
                mpv_network_timeout_seconds INTEGER NOT NULL DEFAULT 15,
                mpv_default_readahead_secs INTEGER NOT NULL DEFAULT 20,
                mpv_extra_options TEXT NOT NULL DEFAULT '',
                playback_auto_switch_source_on_failure INTEGER NOT NULL DEFAULT 0,
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
                player_wide_mode INTEGER NOT NULL DEFAULT 0,
                player_log_visible INTEGER NOT NULL DEFAULT 1,
                preferred_parse_key TEXT NOT NULL DEFAULT '',
                preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_render_mode TEXT NOT NULL DEFAULT 'static',
                preferred_danmaku_color_mode TEXT NOT NULL DEFAULT 'source',
                preferred_danmaku_uniform_color TEXT NOT NULL DEFAULT '#FFFFFF',
                preferred_danmaku_position_preset TEXT NOT NULL DEFAULT 'top',
                preferred_danmaku_scroll_speed REAL NOT NULL DEFAULT 1.0,
                preferred_danmaku_font_size INTEGER NOT NULL DEFAULT 32,
                main_window_geometry BLOB,
                player_window_geometry BLOB,
                player_main_splitter_state BLOB,
                browse_content_splitter_state BLOB,
                last_selected_tab TEXT NOT NULL DEFAULT 'douban',
                last_selected_category_tab TEXT NOT NULL DEFAULT '',
                last_selected_category_id TEXT NOT NULL DEFAULT '',
                global_search_history TEXT NOT NULL DEFAULT '[]',
                global_search_hot_source TEXT NOT NULL DEFAULT '360'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO app_config (
                id, base_url, username, token, vod_token, theme_mode,
                metadata_enhancement_enabled, episode_title_enhancement_enabled,
                metadata_douban_cookie, metadata_tmdb_api_key, metadata_bangumi_access_token,
                network_proxy_mode, network_proxy_url, network_proxy_bypass_rules,
                youtube_cookie_browser, mpv_cache_size_mb, mpv_hwdec_mode,
                mpv_network_timeout_seconds, mpv_default_readahead_secs, mpv_extra_options,
                playback_auto_switch_source_on_failure, last_path, last_active_window,
                last_playback_source, last_playback_source_key, last_playback_mode,
                last_playback_path, last_playback_vod_id, last_playback_clicked_vod_id,
                last_player_paused, player_volume, player_muted, player_wide_mode,
                player_log_visible, preferred_parse_key, preferred_danmaku_enabled,
                preferred_danmaku_line_count, preferred_danmaku_render_mode,
                preferred_danmaku_color_mode, preferred_danmaku_uniform_color,
                preferred_danmaku_position_preset, preferred_danmaku_scroll_speed,
                preferred_danmaku_font_size, main_window_geometry, player_window_geometry,
                player_main_splitter_state, browse_content_splitter_state, last_selected_tab,
                last_selected_category_tab, last_selected_category_id, global_search_history,
                global_search_hot_source
            )
            VALUES (
                1, 'http://127.0.0.1:4567', '', '', '', 'system',
                1, 1, '', '', '', 'direct', '', '[]', '', 512, 'auto-safe',
                15, 20, '', 0, '/', 'main', 'browse', '', '', '', '', 
                '', 0, 100, 0, 0, 1, '', 1, 1, 'static', 'source', '#FFFFFF',
                'top', 1.0, 32, NULL, NULL, NULL, NULL, 'douban', '', '', '[]',
                '360'
            )
            """
        )

    repo = SettingsRepository(db_path)

    assert repo.load_config().logging_enabled is True


def test_settings_repository_migrates_missing_network_proxy_columns(tmp_path: Path) -> None:
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
                metadata_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                episode_title_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                metadata_douban_cookie TEXT NOT NULL DEFAULT '',
                metadata_tmdb_api_key TEXT NOT NULL DEFAULT '',
                metadata_bangumi_access_token TEXT NOT NULL DEFAULT '',
                last_path TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO app_config (
                id, base_url, username, token, vod_token, metadata_enhancement_enabled,
                episode_title_enhancement_enabled, metadata_douban_cookie,
                metadata_tmdb_api_key, metadata_bangumi_access_token, last_path
            )
            VALUES (1, 'http://127.0.0.1:4567', '', '', '', 1, 1, '', '', '', '/')
            """
        )

    config = SettingsRepository(db_path).load_config()

    assert config.network_proxy_mode == "direct"
    assert config.network_proxy_url == ""
    assert config.network_proxy_bypass_rules == [
        "localhost",
        "127.0.0.1",
        "::1",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        ".local",
    ]


def test_settings_repository_migrates_missing_global_search_hot_source_column(tmp_path: Path) -> None:
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
                player_wide_mode INTEGER NOT NULL DEFAULT 0,
                player_log_visible INTEGER NOT NULL DEFAULT 1,
                preferred_parse_key TEXT NOT NULL DEFAULT '',
                preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_render_mode TEXT NOT NULL DEFAULT 'static',
                preferred_danmaku_color_mode TEXT NOT NULL DEFAULT 'source',
                preferred_danmaku_uniform_color TEXT NOT NULL DEFAULT '#FFFFFF',
                preferred_danmaku_position_preset TEXT NOT NULL DEFAULT 'top',
                preferred_danmaku_scroll_speed REAL NOT NULL DEFAULT 1.0,
                preferred_danmaku_font_size INTEGER NOT NULL DEFAULT 32,
                main_window_geometry BLOB,
                player_window_geometry BLOB,
                player_main_splitter_state BLOB,
                browse_content_splitter_state BLOB,
                last_selected_tab TEXT NOT NULL DEFAULT 'douban',
                last_selected_category_tab TEXT NOT NULL DEFAULT '',
                last_selected_category_id TEXT NOT NULL DEFAULT '',
                global_search_history TEXT NOT NULL DEFAULT '[]'
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
                player_muted, player_wide_mode, player_log_visible, preferred_parse_key,
                preferred_danmaku_enabled, preferred_danmaku_line_count,
                preferred_danmaku_render_mode, preferred_danmaku_color_mode,
                preferred_danmaku_uniform_color, preferred_danmaku_position_preset,
                preferred_danmaku_scroll_speed, preferred_danmaku_font_size,
                main_window_geometry, player_window_geometry, player_main_splitter_state,
                browse_content_splitter_state, last_selected_tab, last_selected_category_tab,
                last_selected_category_id, global_search_history
            )
            VALUES (
                1, 'http://127.0.0.1:4567', '', '', '', '/', 'main', 'browse', '', '', '', '', '',
                0, 100, 0, 0, 1, '', 1, 1, 'static', 'source', '#FFFFFF', 'top', 1.0, 32,
                NULL, NULL, NULL, NULL, 'douban', '', '', '[]'
            )
            """
        )

    repo = SettingsRepository(db_path)

    assert repo.load_config().global_search_hot_source == "360"


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


def test_settings_repository_loads_new_danmaku_render_defaults(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")

    config = repo.load_config()

    assert config.preferred_danmaku_render_mode == "static"
    assert config.preferred_danmaku_color_mode == "source"
    assert config.preferred_danmaku_uniform_color == "#FFFFFF"
    assert config.preferred_danmaku_position_preset == "top"
    assert config.preferred_danmaku_scroll_speed == 1.0
    assert config.preferred_danmaku_font_size == 32


def test_settings_repository_persists_new_danmaku_render_settings(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()
    config.preferred_danmaku_render_mode = "mixed"
    config.preferred_danmaku_color_mode = "source"
    config.preferred_danmaku_uniform_color = "#00FF00"
    config.preferred_danmaku_position_preset = "mid_upper"
    config.preferred_danmaku_scroll_speed = 0.8
    config.preferred_danmaku_font_size = 40
    config.preferred_danmaku_line_count = 8

    repo.save_config(config)

    reloaded = repo.load_config()

    assert reloaded.preferred_danmaku_render_mode == "mixed"
    assert reloaded.preferred_danmaku_color_mode == "source"
    assert reloaded.preferred_danmaku_uniform_color == "#00FF00"
    assert reloaded.preferred_danmaku_position_preset == "mid_upper"
    assert reloaded.preferred_danmaku_scroll_speed == 0.8
    assert reloaded.preferred_danmaku_font_size == 40
    assert reloaded.preferred_danmaku_line_count == 8


def test_settings_repository_round_trip_persists_player_wide_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        player_wide_mode=True,
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.player_wide_mode is True
    assert saved == config


def test_settings_repository_round_trip_persists_player_window_geometry_and_log_visibility(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        player_log_visible=False,
        player_window_geometry=b"player-geometry",
        player_main_splitter_state=b"split-main",
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.player_log_visible is False
    assert saved.player_window_geometry == b"player-geometry"
    assert saved.player_main_splitter_state == b"split-main"
    assert saved == config


def test_settings_repository_round_trip_persists_logging_enabled(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(logging_enabled=False)

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.logging_enabled is False
    assert saved == config


def test_settings_repository_defaults_logging_enabled_to_true(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")

    assert repo.load_config().logging_enabled is True


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


def test_settings_repository_migrates_missing_player_wide_mode_column(tmp_path: Path) -> None:
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
                player_muted INTEGER NOT NULL DEFAULT 0,
                preferred_parse_key TEXT NOT NULL DEFAULT '',
                preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1,
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
                last_active_window, last_playback_mode, last_playback_path,
                last_playback_vod_id, last_playback_clicked_vod_id,
                last_player_paused, player_volume, player_muted,
                preferred_parse_key, preferred_danmaku_enabled,
                preferred_danmaku_line_count, main_window_geometry,
                player_window_geometry, player_main_splitter_state,
                browse_content_splitter_state
            )
            VALUES (1, 'http://127.0.0.1:4567', 'alice', '', '', '/TV', 'player', 'detail', '/TV', 'vod-1', 'vod-1', 0, 100, 0, '', 1, 1, NULL, NULL, NULL, NULL)
            """
        )

    repo = SettingsRepository(db_path)
    saved = repo.load_config()

    assert saved.player_wide_mode is False


def test_settings_repository_migrates_missing_last_selected_tab_column(tmp_path: Path) -> None:
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
                player_wide_mode INTEGER NOT NULL DEFAULT 0,
                preferred_parse_key TEXT NOT NULL DEFAULT '',
                preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1,
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
                player_muted, player_wide_mode, preferred_parse_key,
                preferred_danmaku_enabled, preferred_danmaku_line_count,
                main_window_geometry, player_window_geometry,
                player_main_splitter_state, browse_content_splitter_state
            )
            VALUES (1, 'http://127.0.0.1:4567', 'alice', '', '', '/', 'main', 'browse', '', '', '', '', '', 0, 100, 0, 0, '', 1, 1, NULL, NULL, NULL, NULL)
            """
        )

    repo = SettingsRepository(db_path)

    assert repo.load_config().last_selected_tab == "douban"


def test_settings_repository_migrates_missing_last_selected_category_columns(tmp_path: Path) -> None:
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
                player_wide_mode INTEGER NOT NULL DEFAULT 0,
                preferred_parse_key TEXT NOT NULL DEFAULT '',
                preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1,
                main_window_geometry BLOB,
                player_window_geometry BLOB,
                player_main_splitter_state BLOB,
                browse_content_splitter_state BLOB,
                last_selected_tab TEXT NOT NULL DEFAULT 'douban'
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
                player_muted, player_wide_mode, preferred_parse_key,
                preferred_danmaku_enabled, preferred_danmaku_line_count,
                main_window_geometry, player_window_geometry,
                player_main_splitter_state, browse_content_splitter_state,
                last_selected_tab
            )
            VALUES (1, 'http://127.0.0.1:4567', 'alice', '', '', '/', 'main', 'browse', '', '', '', '', '', 0, 100, 0, 0, '', 1, 1, NULL, NULL, NULL, NULL, 'telegram')
            """
        )

    repo = SettingsRepository(db_path)
    saved = repo.load_config()

    assert saved.last_selected_category_tab == ""
    assert saved.last_selected_category_id == ""


def test_settings_repository_migrates_missing_global_search_history_column(tmp_path: Path) -> None:
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
                player_wide_mode INTEGER NOT NULL DEFAULT 0,
                preferred_parse_key TEXT NOT NULL DEFAULT '',
                preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1,
                preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1,
                main_window_geometry BLOB,
                player_window_geometry BLOB,
                player_main_splitter_state BLOB,
                browse_content_splitter_state BLOB,
                last_selected_tab TEXT NOT NULL DEFAULT 'douban',
                last_selected_category_tab TEXT NOT NULL DEFAULT '',
                last_selected_category_id TEXT NOT NULL DEFAULT ''
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
                player_muted, player_wide_mode, preferred_parse_key,
                preferred_danmaku_enabled, preferred_danmaku_line_count,
                main_window_geometry, player_window_geometry,
                player_main_splitter_state, browse_content_splitter_state,
                last_selected_tab, last_selected_category_tab, last_selected_category_id
            )
            VALUES (1, 'http://127.0.0.1:4567', 'alice', '', '', '/', 'main', 'browse', '', '', '', '', '', 0, 100, 0, 0, '', 1, 1, NULL, NULL, NULL, NULL, 'douban', '', '')
            """
        )

    repo = SettingsRepository(db_path)
    saved = repo.load_config()

    assert saved.global_search_history == []


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


def test_spider_plugin_repository_reorder_plugins_rewrites_final_order(tmp_path: Path) -> None:
    repo = SpiderPluginRepository(tmp_path / "app.db")
    plugin1 = repo.add_plugin("local", "/plugins/1.py", "插件1")
    plugin2 = repo.add_plugin("local", "/plugins/2.py", "插件2")
    plugin3 = repo.add_plugin("local", "/plugins/3.py", "插件3")

    repo.reorder_plugins([plugin3.id, plugin1.id, plugin2.id])

    plugins = repo.list_plugins()

    assert [(plugin.id, plugin.sort_order) for plugin in plugins] == [
        (plugin3.id, 0),
        (plugin1.id, 1),
        (plugin2.id, 2),
    ]


def test_spider_plugin_repository_reorder_plugins_rejects_stale_plugin_ids(tmp_path: Path) -> None:
    repo = SpiderPluginRepository(tmp_path / "app.db")
    plugin1 = repo.add_plugin("local", "/plugins/1.py", "插件1")
    plugin2 = repo.add_plugin("local", "/plugins/2.py", "插件2")
    plugin3 = repo.add_plugin("local", "/plugins/3.py", "插件3")

    with pytest.raises(ValueError, match="插件列表已变化"):
        repo.reorder_plugins([plugin3.id, plugin1.id])

    assert [plugin.id for plugin in repo.list_plugins()] == [plugin1.id, plugin2.id, plugin3.id]


def test_spider_plugin_repository_round_trips_category_overrides_json(tmp_path: Path) -> None:
    repo = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repo.add_plugin("local", "/plugins/demo.py", "演示插件")

    repo.set_plugin_category_overrides(
        plugin.id,
        '{"order":["movie","tv"],"hidden":["adult"],"renames":{"movie":"影片"}}',
    )

    saved = repo.get_plugin(plugin.id)

    assert saved.category_overrides_json == (
        '{"order":["movie","tv"],"hidden":["adult"],"renames":{"movie":"影片"}}'
    )


def test_spider_plugin_repository_migrates_missing_category_overrides_column(tmp_path: Path) -> None:
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
                last_error TEXT NOT NULL DEFAULT '',
                config_text TEXT NOT NULL DEFAULT '',
                plugin_version INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO spider_plugins (
                id, source_type, source_value, display_name, enabled, sort_order,
                cached_file_path, last_loaded_at, last_error, config_text, plugin_version
            )
            VALUES (1, 'local', '/plugins/demo.py', '演示插件', 1, 0, '', 0, '', '', 1)
            """
        )

    repo = SpiderPluginRepository(db_path)
    plugin = repo.get_plugin(1)

    assert plugin.category_overrides_json == ""
    repo.set_plugin_category_overrides(1, '{"order":["tv"]}')
    assert repo.get_plugin(1).category_overrides_json == '{"order":["tv"]}'


def test_spider_plugin_repository_partial_updates_preserve_category_overrides_json(tmp_path: Path) -> None:
    repo = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repo.add_plugin("local", "/plugins/demo.py", "演示插件")
    repo.set_plugin_category_overrides(plugin.id, '{"renames":{"movie":"影片"}}')

    repo.update_plugin(
        plugin.id,
        display_name="演示插件新",
        enabled=False,
        cached_file_path="",
        last_loaded_at=1713206400,
        last_error="",
        config_text="token=updated",
    )

    saved = repo.get_plugin(plugin.id)

    assert saved.display_name == "演示插件新"
    assert saved.config_text == "token=updated"
    assert saved.category_overrides_json == '{"renames":{"movie":"影片"}}'


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


def test_spider_plugin_repository_migrates_missing_grouped_source_columns(tmp_path: Path) -> None:
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
                playlist_index INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (plugin_id, vod_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO spider_plugin_playback_history (
                plugin_id, vod_id, vod_name, vod_pic, vod_remarks,
                episode, episode_url, position, opening, ending,
                speed, playlist_index, updated_at
            )
            VALUES (7, 'detail-1', '红果短剧', '', '第1集', 0, 'https://a/1.m3u8', 0, 0, 0, 1.0, 0, 99)
            """
        )

    repo = SpiderPluginRepository(db_path)
    history = repo.get_playback_history(7, "detail-1")

    assert history is not None
    assert history.source_group_index == 0
    assert history.source_index == 0


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


def test_spider_plugin_repository_migrates_missing_category_overrides_column_on_legacy_config_row(
    tmp_path: Path,
) -> None:
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
                last_error TEXT NOT NULL DEFAULT '',
                config_text TEXT NOT NULL DEFAULT '',
                plugin_version INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO spider_plugins (
                source_type, source_value, display_name, enabled, sort_order,
                cached_file_path, last_loaded_at, last_error, config_text, plugin_version
            )
            VALUES ('local', '/plugins/红果短剧.py', '红果短剧', 1, 0, '', 0, '', 'token=updated', 1)
            """
        )

    repo = SpiderPluginRepository(db_path)
    plugin = repo.get_plugin(1)

    assert plugin.category_overrides_json == ""
    repo.set_plugin_category_overrides(1, '{"order":["tv"]}')
    assert repo.get_plugin(1).category_overrides_json == '{"order":["tv"]}'


def test_spider_plugin_repository_partial_updates_do_not_overwrite_other_fields(tmp_path: Path) -> None:
    repo = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repo.add_plugin("local", "/plugins/demo.py", "原名称")

    repo.set_plugin_enabled(plugin.id, False)
    repo.set_plugin_config(plugin.id, "token=updated")
    repo.rename_plugin(plugin.id, "新名称")

    updated = repo.get_plugin(plugin.id)

    assert updated.display_name == "新名称"
    assert updated.enabled is False
    assert updated.config_text == "token=updated"


def test_settings_repository_persists_youtube_category_source(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()
    config.youtube_category_source_type = "remote"
    config.youtube_category_source_value = "http://example.test/youtube.json"
    config.youtube_category_cache_json = '{"class":[]}'
    config.youtube_category_cache_refreshed_at = 1779500000
    config.youtube_category_cache_error = ""

    repo.save_config(config)
    loaded = SettingsRepository(tmp_path / "app.db").load_config()

    assert loaded.youtube_category_source_type == "remote"
    assert loaded.youtube_category_source_value == "http://example.test/youtube.json"
    assert loaded.youtube_category_cache_json == '{"class":[]}'
    assert loaded.youtube_category_cache_refreshed_at == 1779500000
    assert loaded.youtube_category_cache_error == ""


def test_settings_repository_normalizes_invalid_youtube_category_source(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()
    config.youtube_category_source_type = "unknown"
    config.youtube_category_source_value = "  http://example.test/youtube.json  "
    config.youtube_category_cache_refreshed_at = -5

    repo.save_config(config)
    loaded = repo.load_config()

    assert loaded.youtube_category_source_type == "builtin"
    assert loaded.youtube_category_source_value == "http://example.test/youtube.json"
    assert loaded.youtube_category_cache_refreshed_at == 0


def test_settings_repository_persists_bilibili_grouped_playlist_tree_enabled(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()
    config.bilibili_grouped_playlist_tree_enabled = True

    repo.save_config(config)
    loaded = SettingsRepository(tmp_path / "app.db").load_config()

    assert loaded.bilibili_grouped_playlist_tree_enabled is True


def test_settings_repository_persists_following_episode_grid_columns(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()
    config.following_episode_grid_columns = 3

    repo.save_config(config)
    loaded = SettingsRepository(tmp_path / "app.db").load_config()

    assert loaded.following_episode_grid_columns == 3


def test_settings_repository_normalizes_invalid_following_episode_grid_columns(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()
    config.following_episode_grid_columns = 99

    repo.save_config(config)
    loaded = repo.load_config()

    assert loaded.following_episode_grid_columns == 1


def test_settings_repository_defaults_missing_bilibili_grouped_playlist_tree_enabled_to_false(tmp_path: Path) -> None:
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
                theme_mode TEXT NOT NULL DEFAULT 'system',
                logging_enabled INTEGER NOT NULL DEFAULT 1,
                metadata_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                episode_title_enhancement_enabled INTEGER NOT NULL DEFAULT 1,
                disabled_danmaku_provider_ids TEXT NOT NULL DEFAULT '[]',
                disabled_metadata_provider_ids TEXT NOT NULL DEFAULT '[]',
                metadata_douban_cookie TEXT NOT NULL DEFAULT '',
                metadata_tmdb_api_key TEXT NOT NULL DEFAULT '',
                metadata_bangumi_access_token TEXT NOT NULL DEFAULT '',
                network_proxy_mode TEXT NOT NULL DEFAULT 'direct',
                network_proxy_url TEXT NOT NULL DEFAULT '',
                network_proxy_bypass_rules TEXT NOT NULL DEFAULT '[]',
                network_proxy_rules TEXT NOT NULL DEFAULT '[]',
                youtube_cookie_browser TEXT NOT NULL DEFAULT '',
                youtube_max_height INTEGER NOT NULL DEFAULT 1080,
                youtube_video_codec TEXT NOT NULL DEFAULT 'vp9',
                youtube_default_subtitle_lang TEXT NOT NULL DEFAULT '',
                youtube_default_audio_lang TEXT NOT NULL DEFAULT '',
                youtube_metadata_language TEXT NOT NULL DEFAULT '',
                youtube_region TEXT NOT NULL DEFAULT '',
                youtube_category_source_type TEXT NOT NULL DEFAULT 'builtin',
                youtube_category_source_value TEXT NOT NULL DEFAULT '',
                youtube_category_cache_json TEXT NOT NULL DEFAULT '',
                youtube_category_cache_refreshed_at INTEGER NOT NULL DEFAULT 0,
                youtube_category_cache_error TEXT NOT NULL DEFAULT '',
                mpv_cache_size_mb INTEGER NOT NULL DEFAULT 512,
                mpv_hwdec_mode TEXT NOT NULL DEFAULT 'auto-safe',
                mpv_network_timeout_seconds INTEGER NOT NULL DEFAULT 15,
                mpv_default_readahead_secs INTEGER NOT NULL DEFAULT 20,
                mpv_extra_options TEXT NOT NULL DEFAULT '',
                playback_auto_switch_source_on_failure INTEGER NOT NULL DEFAULT 0,
                m3u_proxy_segment_prefetch_size INTEGER NOT NULL DEFAULT 2,
                last_path TEXT NOT NULL DEFAULT '/'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO app_config (
                id, base_url, username, token, vod_token, theme_mode,
                logging_enabled, metadata_enhancement_enabled, episode_title_enhancement_enabled,
                disabled_danmaku_provider_ids, disabled_metadata_provider_ids,
                metadata_douban_cookie, metadata_tmdb_api_key, metadata_bangumi_access_token,
                network_proxy_mode, network_proxy_url, network_proxy_bypass_rules, network_proxy_rules,
                youtube_cookie_browser, youtube_max_height, youtube_video_codec,
                youtube_default_subtitle_lang, youtube_default_audio_lang, youtube_metadata_language,
                youtube_region, youtube_category_source_type, youtube_category_source_value,
                youtube_category_cache_json, youtube_category_cache_refreshed_at, youtube_category_cache_error,
                mpv_cache_size_mb, mpv_hwdec_mode, mpv_network_timeout_seconds,
                mpv_default_readahead_secs, mpv_extra_options,
                playback_auto_switch_source_on_failure, m3u_proxy_segment_prefetch_size, last_path
            )
            VALUES (
                1, 'http://127.0.0.1:4567', '', '', '', 'system',
                1, 1, 1, '[]', '[]', '', '', '', 'direct', '', '[]', '[]',
                '', 1080, 'vp9', '', '', '', '', 'builtin', '', '', 0, '',
                512, 'auto-safe', 15, 20, '', 0, 2, '/'
            )
            """
        )

    loaded = SettingsRepository(db_path).load_config()

    assert loaded.bilibili_grouped_playlist_tree_enabled is False


def test_app_config_defaults_ai_settings_disabled() -> None:
    config = AppConfig()

    assert config.ai_enabled is False
    assert config.ai_base_url == ""
    assert config.ai_api_key == ""
    assert config.ai_chat_model == ""
    assert config.ai_request_timeout_seconds == 30


def test_settings_repository_saves_ai_provider_config(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()
    config.ai_enabled = True
    config.ai_base_url = "https://api.example.com/v1"
    config.ai_api_key = "sk-test"
    config.ai_chat_model = "gpt-4o-mini"
    config.ai_request_timeout_seconds = 45

    repo.save_config(config)
    saved = SettingsRepository(tmp_path / "app.db").load_config()

    assert saved.ai_enabled is True
    assert saved.ai_base_url == "https://api.example.com/v1"
    assert saved.ai_api_key == "sk-test"
    assert saved.ai_chat_model == "gpt-4o-mini"
    assert saved.ai_request_timeout_seconds == 45


def test_settings_repository_normalizes_ai_values(tmp_path: Path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    repo.save_config(
        AppConfig(
            ai_enabled=True,
            ai_base_url=" https://api.example.com/v1/ ",
            ai_api_key=" sk-test ",
            ai_chat_model=" gpt-4o-mini ",
            ai_request_timeout_seconds=999,
        )
    )

    saved = repo.load_config()

    assert saved.ai_base_url == "https://api.example.com/v1/"
    assert saved.ai_api_key == "sk-test"
    assert saved.ai_chat_model == "gpt-4o-mini"
    assert saved.ai_request_timeout_seconds == 120
