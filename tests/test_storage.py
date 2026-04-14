from pathlib import Path

from atv_player.models import AppConfig
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
        main_window_geometry=None,
        player_window_geometry=None,
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved == config


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
            main_window_geometry=None,
            player_window_geometry=None,
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
