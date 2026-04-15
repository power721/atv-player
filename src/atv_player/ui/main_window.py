from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt, Signal
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

from atv_player.ui.browse_page import BrowsePage
from atv_player.ui.douban_page import DoubanPage
from atv_player.ui.history_page import HistoryPage
from atv_player.ui.player_window import PlayerWindow


class _EmptyDoubanController:
    def load_categories(self):
        return []

    def load_items(self, category_id: str, page: int):
        return [], 0


class MainWindow(QMainWindow):
    logout_requested = Signal()

    def __init__(
        self,
        browse_controller,
        history_controller,
        player_controller,
        config,
        save_config=None,
        douban_controller=None,
    ) -> None:
        super().__init__()
        self._save_config = save_config or (lambda: None)
        self.nav_tabs = QTabWidget()
        self.logout_button = QPushButton("退出登录")
        self.browse_page = BrowsePage(browse_controller, config=config, save_config=self._save_config)
        self.douban_page = DoubanPage(douban_controller or _EmptyDoubanController())
        self.history_page = HistoryPage(history_controller)
        self.browse_controller = browse_controller
        self.player_controller = player_controller
        self.player_window: PlayerWindow | None = None
        self.config = config

        self.nav_tabs.addTab(self.douban_page, "豆瓣电影")
        self.nav_tabs.addTab(self.browse_page, "文件浏览")
        self.nav_tabs.addTab(self.history_page, "播放记录")
        self.logout_button.clicked.connect(self.logout_requested.emit)
        header_layout = QHBoxLayout()
        header_layout.addStretch(1)
        header_layout.addWidget(self.logout_button)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.addLayout(header_layout)
        container_layout.addWidget(self.nav_tabs)
        self.setCentralWidget(container)
        self.setWindowTitle("alist-tvbox Desktop Player")
        if self.config.main_window_geometry:
            self.restoreGeometry(QByteArray(self.config.main_window_geometry))

        self.browse_page.open_requested.connect(self.open_player)
        self.history_page.open_detail_requested.connect(self.open_history_detail)
        self.douban_page.search_requested.connect(self._handle_douban_search_requested)

        self.douban_page.unauthorized.connect(self.logout_requested.emit)
        self.browse_page.unauthorized.connect(self.logout_requested.emit)
        self.history_page.unauthorized.connect(self.logout_requested.emit)
        self.quit_shortcut = QShortcut(QKeySequence.StandardKey.Quit, self)
        self.quit_shortcut.activated.connect(self._quit_application)
        self.player_shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
        self.player_shortcut.activated.connect(self.show_or_restore_player)
        self.escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.escape_shortcut.activated.connect(self.show_or_restore_player)

        if hasattr(browse_controller, "load_folder"):
            self.browse_page.load_path(config.last_path or "/")
        if hasattr(history_controller, "load_page"):
            self.history_page.load_history()

    def show_browse_path(self, path: str) -> None:
        self.nav_tabs.setCurrentWidget(self.browse_page)
        self.browse_page.load_path(path)

    def _handle_douban_search_requested(self, keyword: str) -> None:
        self.nav_tabs.setCurrentWidget(self.browse_page)
        self.browse_page.search_keyword(keyword)

    def open_history_detail(self, vod_id: str) -> None:
        try:
            request = self.browse_controller.build_request_from_detail(vod_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.open_player(request)

    def open_player(self, request, restore_paused_state: bool = False) -> None:
        session = self.player_controller.create_session(
            request.vod,
            request.playlist,
            request.clicked_index,
            detail_resolver=request.detail_resolver,
            resolved_vod_by_id=request.resolved_vod_by_id,
        )
        if self.player_window is None:
            self.player_window = PlayerWindow(self.player_controller, self.config, self._save_config)
            if hasattr(self.player_window, "closed_to_main"):
                self.player_window.closed_to_main.connect(self._show_main_again)
        self.config.last_active_window = "player"
        self.config.last_playback_mode = request.source_mode
        self.config.last_playback_path = request.source_path
        self.config.last_playback_vod_id = request.source_vod_id
        self.config.last_playback_clicked_vod_id = request.source_clicked_vod_id
        start_paused = self.config.last_player_paused if restore_paused_state else False
        if not restore_paused_state:
            self.config.last_player_paused = False
        self.config.main_window_geometry = bytes(self.saveGeometry())
        self._save_config()
        self.player_window.open_session(session, start_paused=start_paused)
        self.player_window.show()
        self.player_window.raise_()
        self.player_window.activateWindow()
        self.hide()

    def _show_main_again(self) -> None:
        self.config.last_active_window = "main"
        self._save_config()
        self.show()
        self.raise_()
        self.activateWindow()

    def show_or_restore_player(self) -> None:
        if self.player_window is not None and getattr(self.player_window, "session", None) is not None:
            self.config.last_active_window = "player"
            self._save_config()
            self.player_window.show()
            self.player_window.raise_()
            self.player_window.activateWindow()
            self.hide()
            return self.player_window
        return self.restore_last_player()

    def restore_last_player(self):
        mode = self.config.last_playback_mode
        if mode == "detail" and self.config.last_playback_vod_id:
            request = self.browse_controller.build_request_from_detail(self.config.last_playback_vod_id)
        elif mode == "folder" and self.config.last_playback_path and self.config.last_playback_clicked_vod_id:
            items, _ = self.browse_controller.load_folder(self.config.last_playback_path)
            clicked = next((item for item in items if item.vod_id == self.config.last_playback_clicked_vod_id), None)
            if clicked is None:
                return None
            request = self.browse_controller.build_request_from_folder_item(clicked, items)
        else:
            return None
        self.open_player(request, restore_paused_state=True)
        return self.player_window

    def show_error(self, message: str) -> None:
        QMessageBox.critical(self, "错误", message)

    def _quit_application(self) -> None:
        self.config.last_active_window = "main"
        self.config.main_window_geometry = bytes(self.saveGeometry())
        self._save_config()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.config.main_window_geometry = bytes(self.saveGeometry())
        if self.isVisible():
            self.config.last_active_window = "main"
        self._save_config()
        super().closeEvent(event)
