import sqlite3
from pathlib import Path

from atv_player.live_epg_repository import LiveEpgRepository


def test_live_epg_repository_creates_default_config_row(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")

    config = repo.load()

    assert config.id == 1
    assert config.epg_url == ""
    assert config.cache_text == ""
    assert config.last_refreshed_at == 0
    assert config.last_error == ""


def test_live_epg_repository_round_trips_url_without_clearing_cache(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_refresh_result(cache_text="<tv />", last_refreshed_at=9, last_error="")

    repo.save_url("https://example.com/epg.xml")

    config = repo.load()
    assert config.epg_url == "https://example.com/epg.xml"
    assert config.cache_text == "<tv />"
    assert config.last_refreshed_at == 9


def test_live_epg_repository_persists_refresh_result(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")

    repo.save_refresh_result(cache_text="<tv>cached</tv>", last_refreshed_at=17, last_error="broken")

    config = repo.load()
    assert config.cache_text == "<tv>cached</tv>"
    assert config.last_refreshed_at == 17
    assert config.last_error == "broken"


def test_live_epg_repository_creates_table_for_existing_database(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE app_config (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO app_config (id) VALUES (1)")

    repo = LiveEpgRepository(db_path)

    assert repo.load().id == 1
