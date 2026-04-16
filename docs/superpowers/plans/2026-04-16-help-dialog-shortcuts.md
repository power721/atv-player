# Help Dialog Shortcuts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `F1` shortcut in both the main window and player window that opens a reusable help dialog showing the relevant shortcut list for the current window.

**Architecture:** Introduce a small shared UI module that owns shortcut-help data and renders a modal `QDialog` with a two-column shortcut table. `MainWindow` and `PlayerWindow` each keep one dialog reference, wire `F1` to a shared entry point, and clear stale references when the dialog closes so repeated activation reuses one visible dialog per window.

**Tech Stack:** Python 3.13, PySide6, pytest-qt, existing `uv run pytest` test workflow

---

## File Map

- Create `src/atv_player/ui/help_dialog.py`
  - Owns `ShortcutEntry`, context-specific shortcut lists, the `ShortcutHelpDialog` widget, and a helper that either reuses or creates a dialog for a given parent/context.
- Modify `src/atv_player/ui/main_window.py`
  - Add `F1` wiring, hold a main-window help dialog reference, and invoke the shared dialog helper.
- Modify `src/atv_player/ui/player_window.py`
  - Add `F1` wiring alongside existing application shortcuts, hold a player-window help dialog reference, and invoke the shared dialog helper.
- Modify `tests/test_app.py`
  - Add focused main-window help-dialog tests and test helpers for locating the visible dialog and reading its shortcut table.
- Modify `tests/test_player_window_ui.py`
  - Add focused player-window help-dialog tests using the same dialog inspection pattern.

### Task 1: Main Window Help Dialog Red Test

**Files:**
- Modify: `tests/test_app.py:1-12`
- Modify: `tests/test_app.py:420-520`

- [ ] **Step 1: Write the failing main-window tests**

Add the helper imports near the top of `tests/test_app.py`:

```python
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QTableWidget
```

Add these helpers and tests near the main-window UI tests:

```python
def visible_shortcut_help_dialogs() -> list[QDialog]:
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, QDialog)
        and widget.windowTitle() == "快捷键帮助"
        and widget.isVisible()
    ]


def shortcut_table_rows(dialog: QDialog) -> list[tuple[str, str]]:
    table = dialog.findChild(QTableWidget, "shortcutHelpTable")
    assert table is not None
    rows: list[tuple[str, str]] = []
    for row in range(table.rowCount()):
        rows.append((table.item(row, 0).text(), table.item(row, 1).text()))
    return rows


def test_main_window_f1_opens_shortcut_help_dialog(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    window.setFocus()

    QTest.keyClick(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    rows = shortcut_table_rows(visible_shortcut_help_dialogs()[0])

    assert ("F1", "打开快捷键帮助") in rows
    assert ("Ctrl+P", "显示或返回播放器") in rows
    assert ("Esc", "显示或返回播放器") in rows
    assert any(description == "退出应用" for _, description in rows)


def test_main_window_reuses_existing_shortcut_help_dialog(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    window.setFocus()

    QTest.keyClick(window, Qt.Key.Key_F1)
    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    first_dialog = visible_shortcut_help_dialogs()[0]

    QTest.keyClick(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    assert visible_shortcut_help_dialogs()[0] is first_dialog
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run:

```bash
uv run pytest tests/test_app.py -k shortcut_help -q
```

Expected: failure because the help dialog module, `F1` shortcut wiring, and `shortcutHelpTable` widget do not exist yet.

- [ ] **Step 3: Commit the failing test baseline**

```bash
git add tests/test_app.py
git commit -m "test: cover main window shortcut help dialog"
```

### Task 2: Shared Help Dialog Module and Main Window Green Pass

**Files:**
- Create: `src/atv_player/ui/help_dialog.py`
- Modify: `src/atv_player/ui/main_window.py:5-22`
- Modify: `src/atv_player/ui/main_window.py:176-192`
- Modify: `src/atv_player/ui/main_window.py:194-260`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the shared help dialog module**

Create `src/atv_player/ui/help_dialog.py` with this implementation:

```python
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

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
```

Before saving, add the missing `Qt` import in the widget import section:

```python
from PySide6.QtCore import Qt
```

- [ ] **Step 2: Wire `F1` into `MainWindow`**

Update the imports and add the dialog state in `src/atv_player/ui/main_window.py`:

```python
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from atv_player.ui.help_dialog import ShortcutHelpDialog, show_shortcut_help_dialog
```

Inside `MainWindow.__init__`, add the dialog reference and the new shortcut:

```python
        self.player_window: PlayerWindow | None = None
        self.help_dialog: ShortcutHelpDialog | None = None
        self.config = config
```

```python
        self.help_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F1), self)
        self.help_shortcut.activated.connect(self._show_shortcut_help)
```

Add these methods on `MainWindow`:

```python
    def _show_shortcut_help(self) -> None:
        dialog = show_shortcut_help_dialog(
            self,
            context="main_window",
            existing_dialog=self.help_dialog,
            quit_sequence=self.quit_shortcut.key(),
        )
        if dialog is self.help_dialog:
            return
        self.help_dialog = dialog
        dialog.destroyed.connect(self._clear_help_dialog_reference)

    def _clear_help_dialog_reference(self, *_args) -> None:
        self.help_dialog = None
```

- [ ] **Step 3: Run the focused main-window tests to verify they pass**

Run:

```bash
uv run pytest tests/test_app.py -k shortcut_help -q
```

Expected: both main-window help-dialog tests pass.

- [ ] **Step 4: Commit the shared dialog and main-window integration**

```bash
git add src/atv_player/ui/help_dialog.py src/atv_player/ui/main_window.py tests/test_app.py
git commit -m "feat: add main window shortcut help dialog"
```

### Task 3: Player Window Help Dialog Red Test

**Files:**
- Modify: `tests/test_player_window_ui.py:1-12`
- Modify: `tests/test_player_window_ui.py:2622-2795`

- [ ] **Step 1: Write the failing player-window tests**

Add the missing imports near the top of `tests/test_player_window_ui.py`:

```python
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QSplitter, QToolTip, QTableWidget
```

Add these helpers and tests near the existing keyboard-shortcut coverage:

```python
def visible_shortcut_help_dialogs() -> list[QDialog]:
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, QDialog)
        and widget.windowTitle() == "快捷键帮助"
        and widget.isVisible()
    ]


def shortcut_table_rows(dialog: QDialog) -> list[tuple[str, str]]:
    table = dialog.findChild(QTableWidget, "shortcutHelpTable")
    assert table is not None
    rows: list[tuple[str, str]] = []
    for row in range(table.rowCount()):
        rows.append((table.item(row, 0).text(), table.item(row, 1).text()))
    return rows


def test_player_window_f1_opens_shortcut_help_dialog(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session())
    window.show()
    window.activateWindow()
    window.setFocus()

    send_key(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    rows = shortcut_table_rows(visible_shortcut_help_dialogs()[0])

    assert ("F1", "打开快捷键帮助") in rows
    assert ("Space", "播放/暂停") in rows
    assert ("Left", "后退 15 秒") in rows
    assert ("Ctrl+Right", "前进 60 秒") in rows
    assert ("M", "静音") in rows
    assert ("Enter", "切换全屏") in rows


def test_player_window_reuses_existing_shortcut_help_dialog(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session())
    window.show()
    window.activateWindow()
    window.setFocus()

    send_key(window, Qt.Key.Key_F1)
    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    first_dialog = visible_shortcut_help_dialogs()[0]

    send_key(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    assert visible_shortcut_help_dialogs()[0] is first_dialog
```

- [ ] **Step 2: Run the focused player-window tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py -k shortcut_help -q
```

Expected: failure because `PlayerWindow` still does not bind `F1` to the shared help dialog.

- [ ] **Step 3: Commit the failing player-window tests**

```bash
git add tests/test_player_window_ui.py
git commit -m "test: cover player shortcut help dialog"
```

### Task 4: Player Window Green Pass

**Files:**
- Modify: `src/atv_player/ui/player_window.py:10-24`
- Modify: `src/atv_player/ui/player_window.py:371-381`
- Modify: `src/atv_player/ui/player_window.py:1179-1207`
- Modify: `src/atv_player/ui/player_window.py:1304-1450`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Add the shared help-dialog import and state**

Update the imports in `src/atv_player/ui/player_window.py`:

```python
from atv_player.ui.help_dialog import ShortcutHelpDialog, show_shortcut_help_dialog
from atv_player.ui.poster_loader import load_remote_poster_image, normalize_poster_url
from atv_player.ui.qt_compat import qbytearray_to_bytes, to_qbytearray
```

In `PlayerWindow.__init__`, add the dialog reference near the other window state:

```python
        self._poster_request_id = 0
        self._video_surface_ready = False
        self._auto_advance_locked = False
        self.help_dialog: ShortcutHelpDialog | None = None
        self._poster_load_signals = _PosterLoadSignals(self)
```

- [ ] **Step 2: Register `F1` in `PlayerWindow` and clear stale references**

Add the new shortcut next to the existing application shortcuts:

```python
        self.help_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F1), self)
        self.help_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.help_shortcut.activated.connect(self._show_shortcut_help)
```

Add these methods below `_quit_application` and before `_return_to_main`:

```python
    def _show_shortcut_help(self) -> None:
        dialog = show_shortcut_help_dialog(
            self,
            context="player_window",
            existing_dialog=self.help_dialog,
            quit_sequence=self.quit_shortcut.key(),
        )
        if dialog is self.help_dialog:
            return
        self.help_dialog = dialog
        dialog.destroyed.connect(self._clear_help_dialog_reference)

    def _clear_help_dialog_reference(self, *_args) -> None:
        self.help_dialog = None
```

Do not add `F1` handling to `keyPressEvent`; keep it owned by `QShortcut` so it matches the other player-wide shortcuts.

- [ ] **Step 3: Run the focused player-window tests to verify they pass**

Run:

```bash
uv run pytest tests/test_player_window_ui.py -k shortcut_help -q
```

Expected: both player-window help-dialog tests pass.

- [ ] **Step 4: Run the keyboard-shortcut regression test**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_keyboard_shortcuts_control_playback_navigation_and_view -q
```

Expected: pass, proving the new `F1` shortcut did not break the existing player controls.

- [ ] **Step 5: Commit the player-window integration**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: add player shortcut help dialog"
```

### Task 5: Focused Verification and Finish

**Files:**
- Verify: `tests/test_app.py`
- Verify: `tests/test_player_window_ui.py`
- Possible fix targets if verification fails: `src/atv_player/ui/help_dialog.py`
- Possible fix targets if verification fails: `src/atv_player/ui/main_window.py`
- Possible fix targets if verification fails: `src/atv_player/ui/player_window.py`
- Possible fix targets if verification fails: `tests/test_app.py`
- Possible fix targets if verification fails: `tests/test_player_window_ui.py`

- [ ] **Step 1: Run the focused help-dialog suite**

Run:

```bash
uv run pytest tests/test_app.py -k shortcut_help -q
uv run pytest tests/test_player_window_ui.py -k shortcut_help -q
```

Expected: all shortcut-help tests pass.

- [ ] **Step 2: Run the broader UI regression slice**

Run:

```bash
uv run pytest tests/test_app.py tests/test_player_window_ui.py -q
```

Expected: the touched UI suites pass without new regressions.

- [ ] **Step 3: Commit any verification-driven fixes**

If verification required code changes, commit them with:

```bash
git add src/atv_player/ui/help_dialog.py src/atv_player/ui/main_window.py src/atv_player/ui/player_window.py tests/test_app.py tests/test_player_window_ui.py
git commit -m "fix: polish shortcut help dialog behavior"
```

If no fixes were needed, skip this commit.
