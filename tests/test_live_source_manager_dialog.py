from atv_player.models import LiveSourceConfig, LiveSourceEntry
from atv_player.ui.live_source_manager_dialog import LiveSourceManagerDialog
from atv_player.ui.manual_live_source_dialog import ManualLiveSourceDialog


class FakeLiveSourceManager:
    def __init__(self) -> None:
        self.sources = [
            LiveSourceConfig(
                id=1,
                source_type="remote",
                source_value="https://example.com/live.m3u",
                display_name="远程源",
                enabled=True,
                sort_order=0,
            ),
            LiveSourceConfig(
                id=2,
                source_type="manual",
                source_value="",
                display_name="手动源",
                enabled=True,
                sort_order=1,
            ),
        ]
        self.entries = {
            2: [
                LiveSourceEntry(
                    id=10,
                    source_id=2,
                    group_name="央视",
                    channel_name="CCTV-1",
                    stream_url="https://live.example/cctv1.m3u8",
                    sort_order=0,
                )
            ]
        }
        self.add_remote_calls = []
        self.add_local_calls = []
        self.add_manual_calls = []
        self.toggle_calls = []
        self.refresh_calls = []

    def list_sources(self):
        return list(self.sources)

    def add_remote_source(self, url: str, display_name: str):
        self.add_remote_calls.append((url, display_name))

    def add_local_source(self, path: str, display_name: str):
        self.add_local_calls.append((path, display_name))

    def add_manual_source(self, display_name: str):
        self.add_manual_calls.append(display_name)

    def refresh_source(self, source_id: int):
        self.refresh_calls.append(source_id)

    def set_source_enabled(self, source_id: int, enabled: bool):
        self.toggle_calls.append((source_id, enabled))

    def list_manual_entries(self, source_id: int):
        return list(self.entries.get(source_id, []))


def test_live_source_manager_dialog_renders_rows_and_actions(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.source_table.selectRow(0)
    monkeypatch.setattr(dialog, "_prompt_remote_source", lambda: ("https://example.com/iptv.m3u", "我的远程源"))

    dialog._add_remote_source()
    dialog._refresh_selected()

    assert dialog.source_table.rowCount() == 2
    assert dialog.source_table.item(0, 0).text() == "远程源"
    assert dialog.source_table.item(0, 1).text() == "远程"
    assert dialog.source_table.item(1, 1).text() == "手动"
    assert manager.add_remote_calls == [("https://example.com/iptv.m3u", "我的远程源")]
    assert manager.refresh_calls == [1]


def test_live_source_manager_dialog_shows_manual_editor_button_for_manual_source(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.source_table.selectRow(1)

    dialog._sync_action_state()

    assert dialog.manage_channels_button.isEnabled() is True


def test_live_source_manager_dialog_toggle_disables_enabled_source(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.source_table.selectRow(0)

    dialog._toggle_selected_enabled()

    assert manager.toggle_calls == [(1, False)]


def test_live_source_manager_dialog_toggle_enables_disabled_source(qtbot) -> None:
    manager = FakeLiveSourceManager()
    manager.sources[0].enabled = False
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.source_table.selectRow(0)

    dialog._toggle_selected_enabled()

    assert manager.toggle_calls == [(1, True)]


def test_manual_live_source_dialog_renders_existing_channels(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = ManualLiveSourceDialog(manager, source_id=2)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.reload_entries()

    assert dialog.entry_table.rowCount() == 1
    assert dialog.entry_table.item(0, 0).text() == "央视"
    assert dialog.entry_table.item(0, 1).text() == "CCTV-1"
