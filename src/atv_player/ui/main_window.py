from __future__ import annotations

from typing import Any

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

from atv_player.ui.browse_page import BrowsePage
from atv_player.ui.help_dialog import ShortcutHelpDialog, show_shortcut_help_dialog
from atv_player.ui.poster_grid_page import PosterGridPage
from atv_player.ui.history_page import HistoryPage
from atv_player.ui.plugin_manager_dialog import PluginManagerDialog
from atv_player.ui.player_window import PlayerWindow
from atv_player.ui.qt_compat import qbytearray_to_bytes, to_qbytearray


class _EmptyDoubanController:
    def load_categories(self):
        return []

    def load_items(self, category_id: str, page: int):
        return [], 0


class _EmptyTelegramController(_EmptyDoubanController):
    def build_request(self, vod_id: str):
        raise ValueError(f"没有可播放的项目: {vod_id}")


class _EmptyLiveController(_EmptyDoubanController):
    def build_request(self, vod_id: str):
        raise ValueError(f"没有可播放的项目: {vod_id}")

    def load_folder_items(self, vod_id: str):
        return [], 0


class _EmptyEmbyController(_EmptyDoubanController):
    def build_request(self, vod_id: str):
        raise ValueError(f"没有可播放的项目: {vod_id}")


class _EmptyJellyfinController(_EmptyDoubanController):
    def build_request(self, vod_id: str):
        raise ValueError(f"没有可播放的项目: {vod_id}")


def _plugin_value(definition: Any, key: str):
    if isinstance(definition, dict):
        return definition.get(key)
    return getattr(definition, key)


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
            telegram_controller=None,
            live_controller=None,
            emby_controller=None,
            jellyfin_controller=None,
            spider_plugins=None,
            plugin_manager=None,
            show_emby_tab: bool = True,
            show_jellyfin_tab: bool = True,
    ) -> None:
        super().__init__()
        self._save_config = save_config or (lambda: None)
        self._plugin_definitions = list(spider_plugins or [])
        self._plugin_manager = plugin_manager
        self._plugin_pages: list[tuple[PosterGridPage, object]] = []
        self.nav_tabs = QTabWidget()
        self.plugin_manager_button = QPushButton("插件管理")
        self.logout_button = QPushButton("退出登录")
        self.browse_page = BrowsePage(browse_controller, config=config, save_config=self._save_config)
        self.douban_page = PosterGridPage(douban_controller or _EmptyDoubanController())
        self.telegram_page = PosterGridPage(
            telegram_controller or _EmptyTelegramController(),
            click_action="open",
            search_enabled=True,
        )
        self.live_page = PosterGridPage(
            live_controller or _EmptyLiveController(),
            click_action="open",
            folder_navigation_enabled=True,
        )
        self.emby_page = None
        if show_emby_tab:
            self.emby_page = PosterGridPage(
                emby_controller or _EmptyEmbyController(),
                click_action="open",
                search_enabled=True,
                folder_navigation_enabled=True,
            )
        self.jellyfin_page = None
        if show_jellyfin_tab:
            self.jellyfin_page = PosterGridPage(
                jellyfin_controller or _EmptyJellyfinController(),
                click_action="open",
                search_enabled=True,
                folder_navigation_enabled=True,
            )
        self.history_page = HistoryPage(history_controller)
        self.browse_controller = browse_controller
        self.telegram_controller = telegram_controller or _EmptyTelegramController()
        self.live_controller = live_controller or _EmptyLiveController()
        self.emby_controller = emby_controller or _EmptyEmbyController()
        self.jellyfin_controller = jellyfin_controller or _EmptyJellyfinController()
        self.player_controller = player_controller
        self.player_window: PlayerWindow | None = None
        self.help_dialog: ShortcutHelpDialog | None = None
        self.config = config

        self.nav_tabs.addTab(self.douban_page, "豆瓣电影")
        self.nav_tabs.addTab(self.telegram_page, "电报影视")
        self.nav_tabs.addTab(self.live_page, "网络直播")
        if self.emby_page is not None:
            self.nav_tabs.addTab(self.emby_page, "Emby")
        if self.jellyfin_page is not None:
            self.nav_tabs.addTab(self.jellyfin_page, "Jellyfin")
        self.nav_tabs.addTab(self.browse_page, "文件浏览")
        self.nav_tabs.addTab(self.history_page, "播放记录")
        self._rebuild_spider_plugin_tabs()
        self.logout_button.clicked.connect(self.logout_requested.emit)
        self.plugin_manager_button.clicked.connect(self._open_plugin_manager)
        header_layout = QHBoxLayout()
        header_layout.addStretch(1)
        header_layout.addWidget(self.plugin_manager_button)
        header_layout.addWidget(self.logout_button)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.addLayout(header_layout)
        container_layout.addWidget(self.nav_tabs)
        self.setCentralWidget(container)
        self.setWindowTitle("alist-tvbox Desktop Player")
        if self.config.main_window_geometry:
            self.restoreGeometry(to_qbytearray(self.config.main_window_geometry))

        self.nav_tabs.currentChanged.connect(self._handle_tab_changed)
        self.browse_page.open_requested.connect(self.open_player)
        self.history_page.open_detail_requested.connect(self.open_history_detail)
        self.douban_page.search_requested.connect(self._handle_douban_search_requested)
        self.telegram_page.open_requested.connect(self._handle_telegram_open_requested)
        self.live_page.item_open_requested.connect(self._handle_live_item_open_requested)
        self.live_page.folder_breadcrumb_requested.connect(
            lambda node_id, kind, index: self._handle_media_breadcrumb_requested(
                self.live_page,
                self.live_controller,
                node_id,
                kind,
                index,
            )
        )
        if self.emby_page is not None:
            self.emby_page.item_open_requested.connect(self._handle_emby_item_open_requested)
            self.emby_page.folder_breadcrumb_requested.connect(
                lambda node_id, kind, index: self._handle_media_breadcrumb_requested(
                    self.emby_page,
                    self.emby_controller,
                    node_id,
                    kind,
                    index,
                )
            )
        if self.jellyfin_page is not None:
            self.jellyfin_page.item_open_requested.connect(self._handle_jellyfin_item_open_requested)
            self.jellyfin_page.folder_breadcrumb_requested.connect(
                lambda node_id, kind, index: self._handle_media_breadcrumb_requested(
                    self.jellyfin_page,
                    self.jellyfin_controller,
                    node_id,
                    kind,
                    index,
                )
            )

        self.douban_page.unauthorized.connect(self.logout_requested.emit)
        self.telegram_page.unauthorized.connect(self.logout_requested.emit)
        self.live_page.unauthorized.connect(self.logout_requested.emit)
        if self.emby_page is not None:
            self.emby_page.unauthorized.connect(self.logout_requested.emit)
        if self.jellyfin_page is not None:
            self.jellyfin_page.unauthorized.connect(self.logout_requested.emit)
        self.browse_page.unauthorized.connect(self.logout_requested.emit)
        self.history_page.unauthorized.connect(self.logout_requested.emit)
        self.quit_shortcut = QShortcut(QKeySequence.StandardKey.Quit, self)
        self.quit_shortcut.activated.connect(self._quit_application)
        self.player_shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
        self.player_shortcut.activated.connect(self.show_or_restore_player)
        self.escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.escape_shortcut.activated.connect(self.show_or_restore_player)
        self.help_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F1), self)
        self.help_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.help_shortcut.activated.connect(self._show_shortcut_help)

        self._handle_tab_changed(self.nav_tabs.currentIndex())

    def show_browse_path(self, path: str) -> None:
        self.browse_page.load_path(path)
        self.nav_tabs.setCurrentWidget(self.browse_page)

    def _handle_tab_changed(self, index: int) -> None:
        widget = self.nav_tabs.widget(index)
        if widget is None:
            return
        if widget is self.douban_page:
            self.douban_page.ensure_loaded()
            return
        if widget is self.telegram_page:
            self.telegram_page.ensure_loaded()
            return
        if widget is self.live_page:
            self.live_page.ensure_loaded()
            return
        if widget is self.emby_page and self.emby_page is not None:
            self.emby_page.ensure_loaded()
            return
        if widget is self.jellyfin_page and self.jellyfin_page is not None:
            self.jellyfin_page.ensure_loaded()
            return
        for page, _controller in self._plugin_pages:
            if widget is page:
                page.ensure_loaded()
                return
        if widget is self.browse_page:
            if hasattr(self.browse_controller, "load_folder"):
                self.browse_page.ensure_loaded(self.config.last_path or "/")
            return
        if widget is self.history_page:
            if hasattr(self.history_page.controller, "load_page"):
                self.history_page.ensure_loaded()

    def _handle_douban_search_requested(self, keyword: str) -> None:
        self.nav_tabs.setCurrentWidget(self.browse_page)
        self.browse_page.search_keyword(keyword)

    def _handle_telegram_open_requested(self, vod_id: str) -> None:
        try:
            request = self.telegram_controller.build_request(vod_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.open_player(request)

    def _handle_live_open_requested(self, vod_id: str) -> None:
        try:
            request = self.live_controller.build_request(vod_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.open_player(request)

    def _handle_live_item_open_requested(self, item) -> None:
        if getattr(item, "vod_tag", "") == "folder":
            self._open_media_folder(self.live_page, self.live_controller, item)
            return
        self._handle_live_open_requested(item.vod_id)

    def _handle_emby_open_requested(self, vod_id: str) -> None:
        try:
            request = self.emby_controller.build_request(vod_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.open_player(request)

    def _handle_emby_item_open_requested(self, item) -> None:
        if getattr(item, "vod_tag", "") == "folder":
            if self.emby_page is not None:
                self._open_media_folder(self.emby_page, self.emby_controller, item)
            return
        self._handle_emby_open_requested(item.vod_id)

    def _handle_jellyfin_open_requested(self, vod_id: str) -> None:
        try:
            request = self.jellyfin_controller.build_request(vod_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.open_player(request)

    def _handle_jellyfin_item_open_requested(self, item) -> None:
        if getattr(item, "vod_tag", "") == "folder":
            if self.jellyfin_page is not None:
                self._open_media_folder(self.jellyfin_page, self.jellyfin_controller, item)
            return
        self._handle_jellyfin_open_requested(item.vod_id)

    def _rebuild_spider_plugin_tabs(self) -> None:
        for page, _controller in self._plugin_pages:
            index = self.nav_tabs.indexOf(page)
            if index >= 0:
                self.nav_tabs.removeTab(index)
            page.deleteLater()
        self._plugin_pages = []
        insert_index = self.nav_tabs.indexOf(self.browse_page)
        for definition in self._plugin_definitions:
            controller = _plugin_value(definition, "controller")
            page = PosterGridPage(
                controller,
                click_action="open",
                search_enabled=bool(_plugin_value(definition, "search_enabled")),
            )
            page.open_requested.connect(
                lambda vod_id, controller=controller: self._open_spider_request(controller, vod_id)
            )
            page.unauthorized.connect(self.logout_requested.emit)
            self.nav_tabs.insertTab(insert_index, page, str(_plugin_value(definition, "title") or "插件"))
            self._plugin_pages.append((page, controller))
            insert_index += 1

    def _open_spider_request(self, controller, vod_id: str) -> None:
        try:
            request = controller.build_request(vod_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.open_player(request)

    def _open_plugin_manager(self) -> None:
        if self._plugin_manager is None:
            return
        dialog = PluginManagerDialog(self._plugin_manager, self)
        dialog.exec()
        load_enabled_plugins = getattr(self._plugin_manager, "load_enabled_plugins", None)
        if callable(load_enabled_plugins):
            self._plugin_definitions = list(load_enabled_plugins())
            self._rebuild_spider_plugin_tabs()

    def _open_media_folder(self, page: PosterGridPage, controller: Any, item: Any) -> None:
        try:
            items, total = controller.load_folder_items(item.vod_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        page.show_items(items, total, page=1, empty_message="当前文件夹暂无内容")
        page.push_folder_breadcrumb(item.vod_id, item.vod_name)

    def _handle_media_breadcrumb_requested(
        self,
        page: PosterGridPage,
        controller: Any,
        node_id: str,
        kind: str,
        index: int,
    ) -> None:
        try:
            if kind == "folder":
                items, total = controller.load_folder_items(node_id)
                empty_message = "当前文件夹暂无内容"
                trim_to_index = index
            else:
                category_id = page.selected_category_id
                if not category_id:
                    return
                items, total = controller.load_items(category_id, 1)
                empty_message = "当前分类暂无内容"
                trim_to_index = 1
        except Exception as exc:
            self.show_error(str(exc))
            return
        page.show_items(items, total, page=1, empty_message=empty_message)
        page.trim_folder_breadcrumbs(trim_to_index)

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
            use_local_history=request.use_local_history,
            playback_loader=request.playback_loader,
            playback_progress_reporter=request.playback_progress_reporter,
            playback_stopper=request.playback_stopper,
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
        self.config.main_window_geometry = qbytearray_to_bytes(self.saveGeometry())
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

    def show_or_restore_player(self) -> PlayerWindow | None:
        if self.player_window is not None and getattr(self.player_window, "session", None) is not None:
            self.config.last_active_window = "player"
            self._save_config()
            if hasattr(self.player_window, "resume_from_main"):
                self.player_window.resume_from_main()
            self.player_window.show()
            self.player_window.raise_()
            self.player_window.activateWindow()
            self.hide()
            return self.player_window
        return self.restore_last_player()

    def restore_last_player(self) -> PlayerWindow | None:
        mode = self.config.last_playback_mode
        try:
            if mode == "detail" and self.config.last_playback_vod_id:
                request = self.browse_controller.build_request_from_detail(self.config.last_playback_vod_id)
            elif mode == "folder" and self.config.last_playback_path and self.config.last_playback_clicked_vod_id:
                clicked, items = self._find_restorable_folder_item(
                    self.config.last_playback_path,
                    self.config.last_playback_clicked_vod_id,
                )
                if clicked is None:
                    return None
                request = self.browse_controller.build_request_from_folder_item(clicked, items)
            else:
                return None
        except Exception:
            return None
        self.open_player(request, restore_paused_state=True)
        return self.player_window

    def _find_restorable_folder_item(
        self,
        path: str,
        clicked_vod_id: str,
        page_size: int = 50,
    ) -> tuple[Any | None, list[Any]]:
        page = 1
        total_pages = 1
        while page <= total_pages:
            items, total = self.browse_controller.load_folder(path, page=page, size=page_size)
            clicked = next((item for item in items if item.vod_id == clicked_vod_id), None)
            if clicked is not None:
                return clicked, items
            total_pages = max(1, (total + page_size - 1) // page_size)
            page += 1
        return None, []

    def show_error(self, message: str) -> None:
        QMessageBox.critical(self, "错误", message)

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

    def _quit_application(self) -> None:
        self.config.last_active_window = "main"
        self.config.main_window_geometry = qbytearray_to_bytes(self.saveGeometry())
        self._save_config()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_F1:
            self._show_shortcut_help()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.config.main_window_geometry = qbytearray_to_bytes(self.saveGeometry())
        if self.isVisible():
            self.config.last_active_window = "main"
        self._save_config()
        super().closeEvent(event)
