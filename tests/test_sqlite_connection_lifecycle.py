from __future__ import annotations

from pathlib import Path

import pytest

import atv_player.live_epg_repository as live_epg_repository_module
import atv_player.live_source_repository as live_source_repository_module
import atv_player.local_playback_history as local_playback_history_module
import atv_player.plugins.repository as spider_plugin_repository_module
import atv_player.storage as storage_module


class RecordingConnection:
    def __init__(self) -> None:
        self.close_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def close(self) -> None:
        self.close_calls += 1


@pytest.mark.parametrize(
    ("module", "repository_name"),
    [
        (storage_module, "SettingsRepository"),
        (spider_plugin_repository_module, "SpiderPluginRepository"),
        (live_source_repository_module, "LiveSourceRepository"),
        (live_epg_repository_module, "LiveEpgRepository"),
        (local_playback_history_module, "LocalPlaybackHistoryRepository"),
    ],
)
def test_repository_connect_context_closes_sqlite_connection(monkeypatch, module, repository_name: str) -> None:
    connection = RecordingConnection()
    connect_calls: list[Path] = []

    monkeypatch.setattr(
        module.sqlite3,
        "connect",
        lambda db_path: connect_calls.append(Path(db_path)) or connection,
    )

    repository_cls = getattr(module, repository_name)
    repository = repository_cls.__new__(repository_cls)
    repository._db_path = Path("/tmp/app.db")

    with repository._connect() as active_connection:
        assert active_connection is connection

    assert connect_calls == [Path("/tmp/app.db")]
    assert connection.close_calls == 1
