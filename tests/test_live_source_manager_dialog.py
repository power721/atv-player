import threading
from datetime import datetime

from atv_player.models import LiveSourceConfig, LiveSourceEntry
from atv_player.ui.live_source_manager_dialog import LiveSourceManagerDialog
from atv_player.ui.manual_live_source_dialog import ManualLiveSourceDialog


class FakeLiveSourceManager:
    def __init__(self) -> None:
        self.epg_config = type(
            "Config",
            (),
            {
                "epg_url": "https://example.com/epg-1.xml\nhttps://example.com/epg-2.xml",
                "last_error": "",
                "last_refreshed_at": 12,
            },
        )()
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
        self.save_epg_url_calls = []
        self.refresh_epg_calls = []
        self.add_entry_calls = []
        self.update_entry_calls = []
        self.delete_entry_calls = []
        self.move_entry_calls = []

    def load_epg_config(self):
        return self.epg_config

    def save_epg_url(self, url: str):
        self.save_epg_url_calls.append(url)
        self.epg_config.epg_url = url

    def refresh_epg(self):
        self.refresh_epg_calls.append(None)

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


def test_live_source_manager_dialog_renders_multiline_epg_editor(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)

    assert dialog.epg_url_edit.toPlainText() == (
        "https://example.com/epg-1.xml\nhttps://example.com/epg-2.xml"
    )
    assert dialog.save_epg_button.text() == "保存"
    assert dialog.refresh_epg_button.text() == "立即更新"


def test_live_source_manager_dialog_formats_source_refresh_time(qtbot) -> None:
    manager = FakeLiveSourceManager()
    manager.sources[0].last_refreshed_at = 1_713_168_000
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)

    expected = datetime.fromtimestamp(1_713_168_000).strftime("%Y-%m-%d %H:%M:%S")
    assert dialog.source_table.item(0, 5).text() == expected


def test_live_source_manager_dialog_formats_epg_refresh_time_when_no_error(qtbot) -> None:
    manager = FakeLiveSourceManager()
    manager.epg_config.last_refreshed_at = 1_713_168_000
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)

    expected = datetime.fromtimestamp(1_713_168_000).strftime("%Y-%m-%d %H:%M:%S")
    assert dialog.epg_status_label.text() == expected


def test_live_source_manager_dialog_hides_legacy_refresh_counters(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)

    assert dialog.source_table.item(0, 5).text() == ""
    assert dialog.epg_status_label.text() == ""


def test_live_source_manager_dialog_saves_normalized_multiline_epg_urls(qtbot) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.epg_url_edit.setPlainText(
        "  https://live.example/epg-1.xml  \n\n https://live.example/epg-2.xml.gz \n"
    )

    dialog._save_epg_url()

    assert manager.save_epg_url_calls == [
        "https://live.example/epg-1.xml\nhttps://live.example/epg-2.xml.gz"
    ]


def test_live_source_manager_dialog_refreshes_epg_in_background(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    refreshed = threading.Event()

    def fake_refresh() -> None:
        manager.refresh_epg_calls.append(None)
        refreshed.set()

    monkeypatch.setattr(manager, "refresh_epg", fake_refresh)

    dialog._refresh_epg()

    qtbot.waitUntil(refreshed.is_set, timeout=1000)


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


def test_live_source_manager_dialog_prompts_for_generic_live_source_url(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    asked = {}

    def fake_get_text(parent, title, label, **kwargs):
        del parent, kwargs
        asked["title"] = title
        asked["label"] = label
        return "https://example.com/live.txt", True

    monkeypatch.setattr("atv_player.ui.live_source_manager_dialog.QInputDialog.getText", fake_get_text)

    assert dialog._prompt_remote_source() == "https://example.com/live.txt"
    assert asked == {"title": "添加远程源", "label": "直播源 URL"}


def test_live_source_manager_dialog_local_picker_accepts_txt_files(qtbot, monkeypatch) -> None:
    manager = FakeLiveSourceManager()
    dialog = LiveSourceManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    asked = {}

    def fake_pick(parent, title, directory, file_filter):
        del parent, directory
        asked["title"] = title
        asked["filter"] = file_filter
        return "/tmp/iptv.txt", "TXT Files (*.txt)"

    monkeypatch.setattr("atv_player.ui.live_source_manager_dialog.QFileDialog.getOpenFileName", fake_pick)

    assert dialog._pick_local_source() == "/tmp/iptv.txt"
    assert asked == {
        "title": "选择直播源文件",
        "filter": "Live Source Files (*.m3u *.m3u8 *.txt)",
    }


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
