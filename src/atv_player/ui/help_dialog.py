from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from atv_player.dependency_installer import DependencyInstallError, install_dependency
from atv_player.diagnostics import SystemInfoEntry
from atv_player.ui.external_links import external_link_html
from atv_player.ui.window_chrome import ThemedDialogBase

HelpContext = Literal["main_window", "player_window"]


@dataclass(frozen=True, slots=True)
class ShortcutEntry:
    key: str
    description: str


_MAIN_WINDOW_SHORTCUTS: tuple[ShortcutEntry, ...] = (
    ShortcutEntry("F1", "打开帮助"),
    ShortcutEntry("Ctrl+P", "显示或返回播放器"),
    ShortcutEntry("Esc", "显示或返回播放器"),
)

_PLAYER_WINDOW_SHORTCUTS: tuple[ShortcutEntry, ...] = (
    ShortcutEntry("F1", "打开帮助"),
    ShortcutEntry("Space", "播放/暂停"),
    ShortcutEntry("Enter", "切换全屏"),
    ShortcutEntry("W", "切换宽屏"),
    ShortcutEntry("D", "打开弹幕源"),
    ShortcutEntry("B", "发送弹幕(B站)"),
    ShortcutEntry("S", "打开刮削"),
    ShortcutEntry("C", "搜索外部字幕"),
    ShortcutEntry("Ctrl+D", "打开弹幕设置"),
    ShortcutEntry("I", "显示视频信息"),
    ShortcutEntry("Ctrl+P", "返回主窗口"),
    ShortcutEntry("Esc", "退出全屏或返回主窗口"),
    ShortcutEntry("PgUp", "播放上一集"),
    ShortcutEntry("PgDn", "播放下一集"),
    ShortcutEntry("Left", "后退 15 秒"),
    ShortcutEntry("Right", "前进 15 秒"),
    ShortcutEntry("Ctrl+Left", "后退 60 秒"),
    ShortcutEntry("Ctrl+Right", "前进 60 秒"),
    ShortcutEntry("Shift+Left", "上一章节"),
    ShortcutEntry("Shift+Right", "下一章节"),
    ShortcutEntry("Up", "音量增加"),
    ShortcutEntry("Down", "音量减小"),
    ShortcutEntry("M", "静音"),
    ShortcutEntry("-", "降低倍速"),
    ShortcutEntry("+", "提高倍速"),
    ShortcutEntry("=", "恢复 1.0x"),
)

_INSTALL_LINK_COMPONENTS = frozenset({"Node.js", "yt-dlp"})
_MISSING_SYSTEM_INFO_VALUE = "未安装"
_INSTALL_LINK_LABEL = "一键安装"


def shortcut_entries_for(context: HelpContext, quit_sequence: QKeySequence) -> tuple[ShortcutEntry, ...]:
    quit_label = quit_sequence.toString(QKeySequence.SequenceFormat.NativeText) or "Ctrl+Q"
    base_entries = _MAIN_WINDOW_SHORTCUTS if context == "main_window" else _PLAYER_WINDOW_SHORTCUTS
    return (*base_entries, ShortcutEntry(quit_label, "退出应用"))


class ShortcutHelpDialog(ThemedDialogBase):
    def __init__(
        self,
        entries: Sequence[ShortcutEntry],
        parent: QWidget | None = None,
        *,
        system_info_rows: Sequence[SystemInfoEntry] | None = None,
        diagnostics_text: str = "",
        detailed_diagnostics_text: str = "",
    ) -> None:
        super().__init__(title="帮助", parent=parent)
        self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._diagnostics_text = diagnostics_text
        self._detailed_diagnostics_text = detailed_diagnostics_text
        if system_info_rows is not None:
            self.resize(640, 520)
        else:
            self.resize(520, 420)

        layout = self.content_layout()

        if system_info_rows is not None:
            layout.addWidget(QLabel("系统信息", self))
            self.system_info_table = QTableWidget(0, 2, self)
            self.system_info_table.setObjectName("systemInfoTable")
            self.system_info_table.setHorizontalHeaderLabels(["组件", "版本"])
            self.system_info_table.verticalHeader().setVisible(False)
            self.system_info_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.system_info_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            self.system_info_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.system_info_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            self.system_info_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.update_system_info(system_info_rows)
            layout.addWidget(self.system_info_table)

            actions = QHBoxLayout()
            actions.addStretch(1)
            self.copy_diagnostics_button = QPushButton("一键复制", self)
            self.copy_diagnostics_button.setObjectName("copyDiagnosticsButton")
            self.copy_diagnostics_button.clicked.connect(self._copy_diagnostics_to_clipboard)
            actions.addWidget(self.copy_diagnostics_button)
            self.export_diagnostics_button = QPushButton("导出诊断信息", self)
            self.export_diagnostics_button.setObjectName("exportDiagnosticsButton")
            self.export_diagnostics_button.clicked.connect(self._export_diagnostics)
            actions.addWidget(self.export_diagnostics_button)
            self.export_detailed_diagnostics_button = QPushButton("导出详细诊断", self)
            self.export_detailed_diagnostics_button.setObjectName("exportDetailedDiagnosticsButton")
            self.export_detailed_diagnostics_button.clicked.connect(self._export_detailed_diagnostics)
            actions.addWidget(self.export_detailed_diagnostics_button)
            layout.addLayout(actions)
            layout.addWidget(QLabel("快捷键", self))

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

        layout.addWidget(self.shortcuts_table)

    def update_system_info(
        self,
        system_info_rows: Sequence[SystemInfoEntry],
        *,
        diagnostics_text: str | None = None,
        detailed_diagnostics_text: str | None = None,
    ) -> None:
        if not hasattr(self, "system_info_table"):
            return
        self.system_info_table.clearContents()
        self.system_info_table.setRowCount(len(system_info_rows))
        for row, entry in enumerate(system_info_rows):
            self.system_info_table.setItem(row, 0, QTableWidgetItem(entry.label))
            if entry.url:
                self.system_info_table.setCellWidget(row, 1, self._build_system_info_link_widget(entry))
                continue
            self.system_info_table.setItem(row, 1, QTableWidgetItem(entry.value))
        if diagnostics_text is not None:
            self._diagnostics_text = diagnostics_text
        if detailed_diagnostics_text is not None:
            self._detailed_diagnostics_text = detailed_diagnostics_text

    def _build_system_info_link_widget(self, entry: SystemInfoEntry) -> QLabel:
        label = QLabel(self.system_info_table)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        label.setOpenExternalLinks(False)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        label.setStyleSheet("background: transparent; padding: 0; margin: 0;")
        label.setText(
            external_link_html(entry.url or "", _system_info_link_label(entry))
        )
        label.setToolTip(entry.url or "")
        if _is_install_link(entry):
            label.linkActivated.connect(
                lambda _href: self._install_dependency(entry.label)
            )
        else:
            label.linkActivated.connect(lambda href: self._open_external_url(QUrl(href)))
        return label

    def _open_external_url(self, url: QUrl) -> None:
        if not QDesktopServices.openUrl(url):
            QMessageBox.warning(self, "错误", f"打开链接失败: {url.toString()}")

    def _install_dependency(self, component: str) -> None:
        try:
            install_dependency(component)
        except DependencyInstallError as exc:
            QMessageBox.warning(self, "安装失败", str(exc))
            return
        QMessageBox.information(
            self,
            "安装完成",
            f"{component} 安装完成，请重新打开帮助检查版本。",
        )

    def _copy_diagnostics_to_clipboard(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._diagnostics_text)

    def _export_diagnostics(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出诊断信息",
            "atv-player-diagnostics.txt",
            "Text Files (*.txt);;All Files (*)",
        )
        if not path:
            return
        try:
            Path(path).write_text(self._diagnostics_text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "错误", f"导出诊断信息失败: {exc}")

    def _export_detailed_diagnostics(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出详细诊断",
            "atv-player-diagnostics-detailed.txt",
            "Text Files (*.txt);;All Files (*)",
        )
        if not path:
            return
        try:
            Path(path).write_text(self._detailed_diagnostics_text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "错误", f"导出详细诊断失败: {exc}")


def show_shortcut_help_dialog(
    parent: QWidget,
    *,
    context: HelpContext,
    existing_dialog: ShortcutHelpDialog | None,
    quit_sequence: QKeySequence,
    system_info_rows: Sequence[SystemInfoEntry] | None = None,
    diagnostics_text: str = "",
    detailed_diagnostics_text: str = "",
) -> ShortcutHelpDialog:
    if existing_dialog is not None:
        existing_dialog.show()
        existing_dialog.raise_()
        existing_dialog.activateWindow()
        return existing_dialog

    dialog = ShortcutHelpDialog(
        shortcut_entries_for(context, quit_sequence),
        parent,
        system_info_rows=system_info_rows,
        diagnostics_text=diagnostics_text,
        detailed_diagnostics_text=detailed_diagnostics_text,
    )
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog


def _system_info_link_label(entry: SystemInfoEntry) -> str:
    if _is_install_link(entry):
        return _INSTALL_LINK_LABEL
    return entry.value


def _is_install_link(entry: SystemInfoEntry) -> bool:
    return (
        entry.label in _INSTALL_LINK_COMPONENTS
        and entry.value == _MISSING_SYSTEM_INFO_VALUE
    )
