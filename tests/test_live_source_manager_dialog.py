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
                    logo_url="https://img.example/cctv1.png",
                    sort_order=0,
                )
            ]
        }
        self.add_remote_calls = []
        self.add_local_calls = []
        self.add_manual_calls = []
        self.rename_calls = []
        self.delete_calls = []
        self.toggle_calls = []
        self.refresh_calls = []
        self.add_entry_calls = []
        self.update_entry_calls = []
        self.delete_entry_calls = []
        self.move_entry_calls = []

    def list_sources(self):
        return list(self.sources)

    def add_remote_source(self, url: str, display_name: str):
        self.add_remote_calls.append((url, display_name))

    def add_local_source(self, path: str, display_name: str):
        self.add_local_calls.append((path, display_name))

    def add_manual_source(self, display_name: str):
        self.add_manual_calls.append(display_name)

    def rename_source(self, source_id: int, display_name: str):
        self.rename_calls.append((source_id, display_name))

    def delete_source(self, source_id: int):
        self.delete_calls.append(source_id)

    def refresh_source(self, source_id: int):
        self.refresh_calls.append(source_id)

    def set_source_enabled(self, source_id: int, enabled: bool):
        self.toggle_calls.append((source_id, enabled))

    def list_manual_entries(self, source_id: int):
        return list(self.entries.get(source_id, []))

    def add_manual_entry(self, source_id: int, *, group_name: str, channel_name: str, stream_url: str, logo_url: str):
        self.add_entry_calls.append((source_id, group_name, channel_name, stream_url, logo_url))

    def update_manual_entry(
        self,
        entry_id: int,
        *,
        group_name: str,
        channel_name: str,
        stream_url: str,
        logo_url: str,
    ):
        self.update_entry_calls.append((entry_id, group_name, channel_name, stream_url, logo_url))

    def delete_manual_entry(self, entry_id: int):
        self.delete_entry_calls.append(entry_id)

    def move_manual_entry(self, entry_id: int, direction: int):
        self.move_entry_calls.append((entry_id, direction))


def test_live_source_manager_dialog_renders_rows_and_actions(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.source_table.selectRow(0)
    monkeypatch.setattr(dialog, "_prompt_remote_source", lambda: "https://example.com/iptv.m3u")

    dialog._add_remote_source()
    dialog._refresh_selected()

    assert dialog.source_table.rowCount() == 2
    assert dialog.source_table.item(0, 0).text() == "远程源"
    assert dialog.source_table.item(0, 1).text() == "远程"
    assert dialog.source_table.item(1, 1).text() == "手动"
    assert manager.add_remote_calls == [("https://example.com/iptv.m3u", "iptv")]
    assert manager.refresh_calls == [1]


def test_live_source_manager_dialog_adds_remote_source_with_name_from_url_filename_ignoring_query(
    qtbot,
    monkeypatch,
) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    monkeypatch.setattr(dialog, "_prompt_remote_source", lambda: "https://example.com/live/itv.m3u8?token=1")

    dialog._add_remote_source()

    assert manager.add_remote_calls == [("https://example.com/live/itv.m3u8?token=1", "itv")]


def test_live_source_manager_dialog_adds_remote_source_with_fallback_name_when_url_has_no_filename(
    qtbot,
    monkeypatch,
) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    monkeypatch.setattr(dialog, "_prompt_remote_source", lambda: "https://example.com/live/")

    dialog._add_remote_source()

    assert manager.add_remote_calls == [("https://example.com/live/", "直播源")]


def test_live_source_manager_dialog_adds_local_source_with_name_from_path_stem(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    monkeypatch.setattr(dialog, "_pick_local_source", lambda: "/tmp/my.live.m3u8")

    dialog._add_local_source()

    assert manager.add_local_calls == [("/tmp/my.live.m3u8", "my.live")]


def test_live_source_manager_dialog_shows_manual_editor_button_for_manual_source(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.source_table.selectRow(1)

    dialog._sync_action_state()

    assert dialog.manage_channels_button.isEnabled() is True


def test_live_source_manager_dialog_opens_manual_editor_for_selected_source(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.source_table.selectRow(1)
    opened = {}

    def fake_exec(self) -> int:
        opened["source_id"] = self.source_id
        opened["rows"] = self.entry_table.rowCount()
        return 0

    monkeypatch.setattr(ManualLiveSourceDialog, "exec", fake_exec)

    dialog._manage_selected_channels()

    assert opened == {"source_id": 2, "rows": 1}


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


def test_live_source_manager_dialog_disables_rename_and_delete_without_selection(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.source_table.clearSelection()
    dialog._sync_action_state()

    assert dialog.rename_button.isEnabled() is False
    assert dialog.delete_button.isEnabled() is False


def test_live_source_manager_dialog_disables_toggle_and_refresh_without_selection(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.source_table.clearSelection()
    dialog._sync_action_state()

    assert dialog.toggle_button.isEnabled() is False
    assert dialog.refresh_button.isEnabled() is False


def test_live_source_manager_dialog_renames_selected_source(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.source_table.selectRow(0)
    monkeypatch.setattr(dialog, "_prompt_rename_source", lambda current: "新的名称")

    dialog._rename_selected()

    assert manager.rename_calls == [(1, "新的名称")]


def test_live_source_manager_dialog_deletes_selected_source_after_confirmation(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.source_table.selectRow(0)
    monkeypatch.setattr(dialog, "_confirm_delete_source", lambda name: True)

    dialog._delete_selected()

    assert manager.delete_calls == [1]


def test_manual_live_source_dialog_renders_existing_channels(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = ManualLiveSourceDialog(manager, source_id=2)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.reload_entries()

    assert dialog.entry_table.rowCount() == 1
    assert dialog.entry_table.item(0, 0).text() == "央视"
    assert dialog.entry_table.item(0, 1).text() == "CCTV-1"
    assert dialog.entry_table.item(0, 3).text() == "https://img.example/cctv1.png"


def test_manual_live_source_dialog_adds_entry(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = ManualLiveSourceDialog(manager, source_id=2)
    qtbot.addWidget(dialog)
    dialog.show()
    monkeypatch.setattr(
        dialog,
        "_prompt_entry",
        lambda **kwargs: ("卫视", "湖南卫视", "https://live.example/hunan.m3u8", "https://img.example/hunan.png"),
    )

    dialog._add_entry()

    assert manager.add_entry_calls == [
        (2, "卫视", "湖南卫视", "https://live.example/hunan.m3u8", "https://img.example/hunan.png")
    ]


def test_manual_live_source_dialog_edits_selected_entry(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = ManualLiveSourceDialog(manager, source_id=2)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.reload_entries()
    dialog.entry_table.selectRow(0)
    monkeypatch.setattr(
        dialog,
        "_prompt_entry",
        lambda **kwargs: ("央视", "CCTV-1综合", "https://live.example/cctv1hd.m3u8", "https://img.example/cctv1hd.png"),
    )

    dialog._edit_selected_entry()

    assert manager.update_entry_calls == [
        (10, "央视", "CCTV-1综合", "https://live.example/cctv1hd.m3u8", "https://img.example/cctv1hd.png")
    ]


def test_manual_live_source_dialog_deletes_selected_entry(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = ManualLiveSourceDialog(manager, source_id=2)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.reload_entries()
    dialog.entry_table.selectRow(0)
    monkeypatch.setattr(dialog, "_confirm_delete_entry", lambda channel_name: True)

    dialog._delete_selected_entry()

    assert manager.delete_entry_calls == [10]


def test_manual_live_source_dialog_moves_selected_entry(qtbot) -> None:
    manager = FakeLiveSourceManager()
    manager.entries[2].append(
        LiveSourceEntry(
            id=11,
            source_id=2,
            group_name="央视",
            channel_name="CCTV-2",
            stream_url="https://live.example/cctv2.m3u8",
            logo_url="",
            sort_order=1,
        )
    )
    dialog = ManualLiveSourceDialog(manager, source_id=2)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.reload_entries()
    dialog.entry_table.selectRow(1)

    dialog._move_selected_entry(-1)

    assert manager.move_entry_calls == [(11, -1)]


def test_manual_live_source_dialog_disables_move_buttons_at_list_edges(qtbot) -> None:
    manager = FakeLiveSourceManager()
    manager.entries[2].append(
        LiveSourceEntry(
            id=11,
            source_id=2,
            group_name="央视",
            channel_name="CCTV-2",
            stream_url="https://live.example/cctv2.m3u8",
            logo_url="",
            sort_order=1,
        )
    )
    dialog = ManualLiveSourceDialog(manager, source_id=2)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.reload_entries()

    dialog.entry_table.selectRow(0)
    dialog._sync_action_state()
    assert dialog.up_button.isEnabled() is False
    assert dialog.down_button.isEnabled() is True

    dialog.entry_table.selectRow(1)
    dialog._sync_action_state()
    assert dialog.up_button.isEnabled() is True
    assert dialog.down_button.isEnabled() is False


def test_manual_live_source_dialog_keeps_selection_on_moved_entry(qtbot) -> None:
    class ReorderingLiveSourceManager(FakeLiveSourceManager):
        def move_manual_entry(self, entry_id: int, direction: int):
            super().move_manual_entry(entry_id, direction)
            entries = self.entries[2]
            index = next(i for i, entry in enumerate(entries) if entry.id == entry_id)
            target = index + direction
            if not (0 <= target < len(entries)):
                return
            entries[index], entries[target] = entries[target], entries[index]

    manager = ReorderingLiveSourceManager()
    manager.entries[2].append(
        LiveSourceEntry(
            id=11,
            source_id=2,
            group_name="央视",
            channel_name="CCTV-2",
            stream_url="https://live.example/cctv2.m3u8",
            logo_url="",
            sort_order=1,
        )
    )
    dialog = ManualLiveSourceDialog(manager, source_id=2)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.reload_entries()
    dialog.entry_table.selectRow(0)

    dialog._move_selected_entry(1)

    assert dialog.entry_table.currentRow() == 1
    assert dialog._selected_entry_id() == 10
