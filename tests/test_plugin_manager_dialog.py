from atv_player.models import SpiderPluginConfig, SpiderPluginLogEntry
from atv_player.ui.plugin_manager_dialog import PluginManagerDialog


class FakePluginManager:
    def __init__(self) -> None:
        self.plugins = [
            SpiderPluginConfig(id=1, source_type="local", source_value="/plugins/a.py", display_name="本地A", enabled=True, sort_order=0),
            SpiderPluginConfig(id=2, source_type="remote", source_value="https://example.com/b.py", display_name="远程B", enabled=False, sort_order=1, last_error="下载失败"),
        ]
        self.logs = {
            2: [SpiderPluginLogEntry(id=1, plugin_id=2, level="error", message="下载失败", created_at=1713206400)]
        }
        self.rename_calls: list[tuple[int, str]] = []
        self.toggle_calls: list[tuple[int, bool]] = []
        self.move_calls: list[tuple[int, int]] = []
        self.refresh_calls: list[int] = []
        self.add_local_calls: list[str] = []
        self.add_remote_calls: list[str] = []
        self.delete_calls: list[int] = []

    def list_plugins(self):
        return list(self.plugins)

    def add_local_plugin(self, path: str) -> None:
        self.add_local_calls.append(path)

    def add_remote_plugin(self, url: str) -> None:
        self.add_remote_calls.append(url)

    def rename_plugin(self, plugin_id: int, display_name: str) -> None:
        self.rename_calls.append((plugin_id, display_name))

    def set_plugin_enabled(self, plugin_id: int, enabled: bool) -> None:
        self.toggle_calls.append((plugin_id, enabled))

    def move_plugin(self, plugin_id: int, direction: int) -> None:
        self.move_calls.append((plugin_id, direction))

    def refresh_plugin(self, plugin_id: int) -> None:
        self.refresh_calls.append(plugin_id)

    def delete_plugin(self, plugin_id: int) -> None:
        self.delete_calls.append(plugin_id)

    def list_logs(self, plugin_id: int):
        return self.logs.get(plugin_id, [])


def test_plugin_manager_dialog_renders_rows_and_status(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    assert dialog.plugin_table.rowCount() == 2
    assert dialog.plugin_table.item(0, 0).text() == "本地A"
    assert dialog.plugin_table.item(1, 4).text() == "下载失败"


def test_plugin_manager_dialog_actions_call_manager(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.plugin_table.selectRow(1)

    monkeypatch.setattr(dialog, "_prompt_display_name", lambda current: "远程重命名")
    monkeypatch.setattr(dialog, "_pick_local_plugin_path", lambda: "/plugins/红果短剧.py")
    monkeypatch.setattr(dialog, "_prompt_remote_url", lambda: "https://example.com/红果短剧.py")
    dialog._add_local_plugin()
    dialog._add_remote_plugin()
    dialog._rename_selected()
    dialog._toggle_selected_enabled()
    dialog._move_selected(-1)
    dialog._refresh_selected()
    dialog._delete_selected()

    assert manager.add_local_calls == ["/plugins/红果短剧.py"]
    assert manager.add_remote_calls == ["https://example.com/红果短剧.py"]
    assert manager.rename_calls == [(2, "远程重命名")]
    assert manager.toggle_calls == [(2, True)]
    assert manager.move_calls == [(2, -1)]
    assert manager.refresh_calls == [2]
    assert manager.delete_calls == [2]
