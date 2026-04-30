from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

HelpContext = Literal["main_window", "player_window"]


@dataclass(frozen=True, slots=True)
class ShortcutEntry:
    key: str
    description: str


_MAIN_WINDOW_SHORTCUTS: tuple[ShortcutEntry, ...] = (
    ShortcutEntry("F1", "打开快捷键帮助"),
    ShortcutEntry("Ctrl+P", "显示或返回播放器"),
    ShortcutEntry("Esc", "显示或返回播放器"),
)

_PLAYER_WINDOW_SHORTCUTS: tuple[ShortcutEntry, ...] = (
    ShortcutEntry("F1", "打开快捷键帮助"),
    ShortcutEntry("Space", "播放/暂停"),
    ShortcutEntry("Enter", "切换全屏"),
    ShortcutEntry("W", "切换宽屏"),
    ShortcutEntry("D", "打开弹幕源"),
    ShortcutEntry("I", "显示视频信息"),
    ShortcutEntry("Ctrl+P", "返回主窗口"),
    ShortcutEntry("Esc", "退出全屏或返回主窗口"),
    ShortcutEntry("PgUp", "播放上一集"),
    ShortcutEntry("PgDn", "播放下一集"),
    ShortcutEntry("Left", "后退 15 秒"),
    ShortcutEntry("Right", "前进 15 秒"),
    ShortcutEntry("Ctrl+Left", "后退 60 秒"),
    ShortcutEntry("Ctrl+Right", "前进 60 秒"),
    ShortcutEntry("Up", "音量增加"),
    ShortcutEntry("Down", "音量减小"),
    ShortcutEntry("M", "静音"),
    ShortcutEntry("-", "降低倍速"),
    ShortcutEntry("+", "提高倍速"),
    ShortcutEntry("=", "恢复 1.0x"),
)


def shortcut_entries_for(context: HelpContext, quit_sequence: QKeySequence) -> tuple[ShortcutEntry, ...]:
    quit_label = quit_sequence.toString(QKeySequence.SequenceFormat.NativeText) or "Ctrl+Q"
    base_entries = _MAIN_WINDOW_SHORTCUTS if context == "main_window" else _PLAYER_WINDOW_SHORTCUTS
    return (*base_entries, ShortcutEntry(quit_label, "退出应用"))


class ShortcutHelpDialog(QDialog):
    def __init__(self, entries: Sequence[ShortcutEntry], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle("快捷键帮助")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(520, 420)

        self.shortcuts_table = QTableWidget(len(entries), 2, self)
        self.shortcuts_table.setObjectName("shortcutHelpTable")
        self.shortcuts_table.setHorizontalHeaderLabels(["按键", "说明"])
        self.shortcuts_table.verticalHeader().setVisible(False)
        self.shortcuts_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.shortcuts_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.shortcuts_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.shortcuts_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.shortcuts_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        for row, entry in enumerate(entries):
            self.shortcuts_table.setItem(row, 0, QTableWidgetItem(entry.key))
            self.shortcuts_table.setItem(row, 1, QTableWidgetItem(entry.description))

        layout = QVBoxLayout(self)
        layout.addWidget(self.shortcuts_table)


def show_shortcut_help_dialog(
    parent: QWidget,
    *,
    context: HelpContext,
    existing_dialog: ShortcutHelpDialog | None,
    quit_sequence: QKeySequence,
) -> ShortcutHelpDialog:
    if existing_dialog is not None:
        existing_dialog.show()
        existing_dialog.raise_()
        existing_dialog.activateWindow()
        return existing_dialog

    dialog = ShortcutHelpDialog(shortcut_entries_for(context, quit_sequence), parent)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
