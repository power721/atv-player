from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QMainWindow, QMessageBox, QTabWidget

from atv_player.ui.browse_page import BrowsePage
from atv_player.ui.history_page import HistoryPage
from atv_player.ui.player_window import PlayerWindow
from atv_player.ui.search_page import SearchPage


class MainWindow(QMainWindow):
    logout_requested = Signal()

    def __init__(self, browse_controller, history_controller, player_controller, config) -> None:
        super().__init__()
        self.nav_tabs = QTabWidget()
        self.browse_page = BrowsePage(browse_controller)
        self.search_page = SearchPage(browse_controller)
        self.history_page = HistoryPage(history_controller)
        self.browse_controller = browse_controller
        self.player_controller = player_controller
        self.player_window: PlayerWindow | None = None
        self.config = config

        self.nav_tabs.addTab(self.browse_page, "浏览")
        self.nav_tabs.addTab(self.search_page, "搜索")
        self.nav_tabs.addTab(self.history_page, "播放记录")
        self.setCentralWidget(self.nav_tabs)
        self.setWindowTitle("alist-tvbox Desktop Player")

        self.browse_page.open_requested.connect(self.open_player)
        self.search_page.browse_requested.connect(self.show_browse_path)
        self.history_page.open_detail_requested.connect(self.open_history_detail)

        self.browse_page.unauthorized.connect(self.logout_requested.emit)
        self.search_page.unauthorized.connect(self.logout_requested.emit)
        self.history_page.unauthorized.connect(self.logout_requested.emit)

        if hasattr(browse_controller, "load_folder"):
            self.browse_page.load_path(config.last_path or "/")
        if hasattr(history_controller, "load_page"):
            self.history_page.load_history()

    def show_browse_path(self, path: str) -> None:
        self.nav_tabs.setCurrentWidget(self.browse_page)
        self.browse_page.load_path(path)

    def open_history_detail(self, vod_id: str) -> None:
        try:
            request = self.browse_controller.build_request_from_detail(vod_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.open_player(request)

    def open_player(self, request) -> None:
        session = self.player_controller.create_session(
            request.vod,
            request.playlist,
            request.clicked_index,
        )
        if self.player_window is None:
            self.player_window = PlayerWindow(self.player_controller)
        self.player_window.open_session(session)
        self.player_window.show()
        self.player_window.raise_()
        self.player_window.activateWindow()

    def show_error(self, message: str) -> None:
        QMessageBox.critical(self, "错误", message)
