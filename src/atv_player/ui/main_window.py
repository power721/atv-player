from __future__ import annotations
import inspect
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlparse

import httpx
from PySide6.QtCore import QObject, QTimer, Qt, QUrl, Signal, QSize, QPoint, QEvent
from PySide6.QtGui import QCloseEvent, QDesktopServices, QKeySequence, QShortcut, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from atv_player.controllers.browse_controller import _map_vod_item
from atv_player.controllers.telegram_search_controller import build_detail_playlist
from atv_player.danmaku.direct_parse import DirectParseDanmakuController
from atv_player.diagnostics import collect_system_info_entries
from atv_player.ui.browse_page import BrowsePage
from atv_player.models import (
    HistoryRecord,
    OpenPlayerRequest,
    PlayItem,
    PlaybackDetailFieldAction,
    VodItem,
)
from atv_player.ui.async_guard import AsyncGuardMixin
from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog
from atv_player.ui.help_dialog import ShortcutHelpDialog, shortcut_entries_for, show_shortcut_help_dialog
from atv_player.ui.icon_cache import load_icon
from atv_player.ui.plugin_actions import PluginActions
from atv_player.ui.poster_grid_page import PosterGridPage
from atv_player.ui.history_page import HistoryPage
from atv_player.ui.live_source_manager_dialog import LiveSourceManagerDialog
from atv_player.ui.plugin_manager_dialog import PluginManagerDialog
from atv_player.ui.player_window import PlayerWindow
from atv_player.ui.plugin_tab_drawer import PluginTabDrawer
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


class _EmptyBilibiliController(_EmptyDoubanController):
    def build_request(self, vod_id: str):
        raise ValueError(f"没有可播放的项目: {vod_id}")


class _EmptyJellyfinController(_EmptyDoubanController):
    def build_request(self, vod_id: str):
        raise ValueError(f"没有可播放的项目: {vod_id}")


class _EmptyFeiniuController(_EmptyDoubanController):
    def build_request(self, vod_id: str):
        raise ValueError(f"没有可播放的项目: {vod_id}")


_SUPPORTED_DRIVE_DOMAINS = (
    "alipan.com",
    "aliyundrive.com",
    "mypikpak.com",
    "xunlei.com",
    "123pan.com",
    "123pan.cn",
    "123684.com",
    "123865.com",
    "123912.com",
    "123592.com",
    "quark.cn",
    "139.com",
    "uc.cn",
    "115.com",
    "115cdn.com",
    "anxia.com",
    "189.cn",
    "baidu.com",
)
_DIRECT_PARSE_DETAIL_API = "https://dmku.hls.one/"
_HOTKEY_360_API = "http://api.xcvts.cn/api/hotlist/360so_juhe"
_HOTKEY_TENCENT_API = "https://pbaccess.video.qq.com/trpc.videosearch.hot_rank.HotRankServantHttp/HotRankHttp"
_HOTKEY_IQIYI_API = "https://mesh.if.iqiyi.com/portal/lw/search/keywords/hotList"
_SUGGESTION_360_API = "https://sug.so.360.cn/suggest"
_DEFAULT_GLOBAL_SEARCH_HOT_SOURCE = "360"
_DEFAULT_GLOBAL_SEARCH_HOT_TYPE = "dsp"
_GLOBAL_SEARCH_360_HOT_TABS: list[tuple[str, str]] = [
    ("dsp", "综合"),
    ("movie", "电视剧"),
    ("tv", "电影"),
    ("variety", "综艺"),
    ("comic", "动漫"),
]
_GLOBAL_SEARCH_HOT_SOURCE_ORDER = ["360", "tencent", "iqiyi"]
_GLOBAL_SEARCH_HOT_SOURCE_TITLES = {
    "360": "全网",
    "tencent": "腾讯",
    "iqiyi": "爱奇艺",
}
_GLOBAL_SEARCH_HOT_SOURCE_CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "360": list(_GLOBAL_SEARCH_360_HOT_TABS),
    "tencent": [("hot", "热搜")],
    "iqiyi": [("hot", "热搜")],
}


def _coerce_hot_search_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_hot_search_items(items: object) -> list[dict[str, str]]:
    if not isinstance(items, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = ""
        query = ""
        for key in ("title", "word", "query", "keyword", "name", "text", "hotWord", "searchWord"):
            title = _coerce_hot_search_text(item.get(key))
            if title:
                break
        for key in ("query", "word", "keyword", "title", "name", "text", "hotWord", "searchWord"):
            query = _coerce_hot_search_text(item.get(key))
            if query:
                break
        if not title:
            continue
        normalized.append({"title": title, "query": query or title})
    return normalized


def _iqiyi_hot_category_key(title: str, index: int) -> str:
    normalized = _coerce_hot_search_text(title)
    return _global_search_hot_category_key(normalized, index, "iqiyi")


def _global_search_hot_category_key(title: str, index: int, source: str) -> str:
    normalized = _coerce_hot_search_text(title)
    mapping = {
        "热搜": "hot",
        "电视剧": "movie",
        "电影": "tv",
        "综艺": "variety",
        "动漫": "comic",
        "综合视频": "dsp",
    }
    if normalized in mapping:
        return mapping[normalized]
    ascii_slug = "".join(ch if ch.isalnum() else "_" for ch in normalized.casefold()).strip("_")
    return ascii_slug or f"{source}_{index}"


@dataclass(slots=True)
class _GlobalSearchHotkeyLoadResult:
    source: str
    category: str
    items: list[dict[str, str]]
    categories: list[tuple[str, str]] | None = None


def _plugin_value(definition: Any, key: str):
    if isinstance(definition, dict):
        return definition.get(key)
    return getattr(definition, key)


def _looks_like_http_url(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    parsed = urlparse(candidate)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _looks_like_offline_download_link(value: str) -> bool:
    candidate = value.strip().lower()
    return candidate.startswith("magnet:?") or candidate.startswith("ed2k://")


def _looks_like_drive_share_link(value: str) -> bool:
    candidate = value.strip()
    if not _looks_like_http_url(candidate):
        return False
    hostname = (urlparse(candidate).hostname or "").lower()
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in _SUPPORTED_DRIVE_DOMAINS)


_http_get_loader: Callable[[], Callable[..., Any] | None] | None = None
_http_post_loader: Callable[[], Callable[..., Any] | None] | None = None


def set_main_window_http_get_loader(loader: Callable[[], Callable[..., Any] | None] | None) -> None:
    global _http_get_loader
    _http_get_loader = loader


def set_main_window_http_post_loader(loader: Callable[[], Callable[..., Any] | None] | None) -> None:
    global _http_post_loader
    _http_post_loader = loader


def _resolve_http_get() -> Callable[..., Any]:
    if _http_get_loader is not None:
        resolved = _http_get_loader()
        if resolved is not None:
            return resolved
    return httpx.get


def _resolve_http_post() -> Callable[..., Any]:
    if _http_post_loader is not None:
        resolved = _http_post_loader()
        if resolved is not None:
            return resolved
    return httpx.post


def load_direct_parse_detail(url: str) -> dict[str, Any]:
    response = _resolve_http_get()(
        _DIRECT_PARSE_DETAIL_API,
        params={"ac": "list", "url": url},
        timeout=10.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def load_360_hot_searches(hot_type: str = _DEFAULT_GLOBAL_SEARCH_HOT_TYPE) -> list[dict[str, str]]:
    response = _resolve_http_get()(
        _HOTKEY_360_API,
        params={"type": hot_type},
        timeout=5.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return []
    items = payload.get("data")
    if not isinstance(items, list):
        return []
    hotkeys: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        hotkeys.append({"title": title, "query": title})
    return hotkeys


def load_tencent_hot_searches(hot_type: str = "hot") -> list[dict[str, str]]:
    categories, items_by_category = load_tencent_hot_search_sections()
    if not categories:
        return []
    category_key = hot_type if hot_type in {key for key, _ in categories} else categories[0][0]
    return items_by_category.get(category_key, [])


def load_tencent_hot_search_sections() -> tuple[list[tuple[str, str]], dict[str, list[dict[str, str]]]]:
    response = _resolve_http_post()(
        _HOTKEY_TENCENT_API,
        headers={
            "content-type": "application/json",
            "referer": "https://v.qq.com/",
            "user-agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
        },
        json={
            "pageNum": 0,
            "pageSize": 10,
            "data_version": "25081802",
            "client_type": 2,
        },
        timeout=5.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return [], {}
    nav_items = payload.get("data", {}).get("navItemList")
    if not isinstance(nav_items, list):
        return [], {}
    categories: list[tuple[str, str]] = []
    items_by_category: dict[str, list[dict[str, str]]] = {}
    for item in nav_items:
        if not isinstance(item, dict):
            continue
        title = _coerce_hot_search_text(item.get("tabName") or item.get("title"))
        if not title:
            continue
        hot_rank_result = item.get("hotRankResult")
        normalized: list[dict[str, str]] = []
        if isinstance(hot_rank_result, dict):
            for key in ("rankItemList", "hotRankList", "itemList", "list", "items"):
                normalized = _normalize_hot_search_items(hot_rank_result.get(key))
                if normalized:
                    break
        if not normalized:
            normalized = _normalize_hot_search_items(hot_rank_result)
        if normalized:
            category_key = _global_search_hot_category_key(title, len(categories), "tencent")
            categories.append((category_key, title))
            items_by_category[category_key] = normalized
    return categories, items_by_category


def load_iqiyi_hot_search_sections() -> tuple[list[tuple[str, str]], dict[str, list[dict[str, str]]]]:
    response = _resolve_http_get()(
        _HOTKEY_IQIYI_API,
        params={
            "device_id": "7b16c55cfdf4edb1a33cd4fc07bc0f69",
            "v": "17.052.25283",
            "appMode": "",
            "src": "",
        },
        timeout=5.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return [], {}
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    sections = data.get("hotQuery")
    if not isinstance(sections, list):
        return [], {}
    categories: list[tuple[str, str]] = []
    items_by_category: dict[str, list[dict[str, str]]] = {}
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        title = _coerce_hot_search_text(section.get("title"))
        if not title:
            continue
        key = _iqiyi_hot_category_key(title, index)
        items = _normalize_hot_search_items(section.get("items"))
        categories.append((key, title))
        items_by_category[key] = items
    return categories, items_by_category


def load_global_search_hotkey_payload(
    source: str = _DEFAULT_GLOBAL_SEARCH_HOT_SOURCE,
    hot_type: str = _DEFAULT_GLOBAL_SEARCH_HOT_TYPE,
) -> _GlobalSearchHotkeyLoadResult:
    if source == "tencent":
        categories, items_by_category = load_tencent_hot_search_sections()
        if not categories:
            categories = list(_GLOBAL_SEARCH_HOT_SOURCE_CATEGORIES["tencent"])
        category_key = hot_type if hot_type in {key for key, _ in categories} else categories[0][0]
        return _GlobalSearchHotkeyLoadResult(
            source=source,
            category=category_key,
            items=items_by_category.get(category_key, []),
            categories=categories,
        )
    if source == "iqiyi":
        categories, items_by_category = load_iqiyi_hot_search_sections()
        if not categories:
            categories = list(_GLOBAL_SEARCH_HOT_SOURCE_CATEGORIES["iqiyi"])
        category_key = hot_type if hot_type in {key for key, _ in categories} else categories[0][0]
        return _GlobalSearchHotkeyLoadResult(
            source=source,
            category=category_key,
            items=items_by_category.get(category_key, []),
            categories=categories,
        )
    return _GlobalSearchHotkeyLoadResult(
        source=source,
        category=hot_type,
        items=load_360_hot_searches(hot_type),
    )


def load_360_search_suggestions(keyword: str) -> list[str]:
    response = _resolve_http_get()(
        _SUGGESTION_360_API,
        params={"word": keyword, "encodein": "utf-8", "encodeout": "utf-8"},
        timeout=5.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return []
    items = payload.get("result")
    if not isinstance(items, list):
        return []
    suggestions: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("word") or "").strip()
        if not text or text in seen:
            continue
        suggestions.append(text)
        seen.add(text)
    return suggestions


class _PluginController(Protocol):
    def load_categories(self): ...

    def load_items(self, category_id: str, page: int): ...

    def build_request(self, vod_id: str) -> OpenPlayerRequest: ...


class _AsyncRequestSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)


class _RestoreSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int)


class _SessionOpenSignals(QObject):
    succeeded = Signal(int, object, object, bool)
    failed = Signal(int, str, bool)


class _GlobalSearchSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)


class _StartupPluginLoadSignals(QObject):
    loaded = Signal(int, object)
    finished = Signal(int)
    failed = Signal(int, str)


class _GlobalSearchPopupSignals(QObject):
    hotkeys_loaded = Signal(int, object)


class SearchInputWithHotkey(QLineEdit):
    focus_gained = Signal()
    focus_lost = Signal()
    escape_pressed = Signal()
    pressed = Signal()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.focus_gained.emit()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.focus_lost.emit()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self.pressed.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.escape_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class GlobalSearchPopup(QWidget):
    item_clicked = Signal(str)
    clear_history_requested = Signal()
    delete_history_requested = Signal(str)
    hot_source_changed = Signal(str)
    hot_tab_changed = Signal(str)
    HISTORY_ITEM_HEIGHT = 40
    HOT_ITEM_HEIGHT = 48

    _CONTAINER_QSS = """
    QWidget#globalSearchPopupContainer {
        background: #f7f1e8;
        border: 1px solid #dccfbe;
        border-radius: 0;
        color: #2f241c;
    }
    """
    _DIVIDER_QSS = "QFrame { color: #e7d8c8; background: #e7d8c8; min-width: 1px; }"
    _SECTION_TITLE_QSS = """
    QLabel {
        color: #7a5c47;
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 1px;
    }
    """
    _ACTION_BUTTON_QSS = """
    QPushButton {
        color: #a98268;
        font-size: 12px;
        border: none;
        background: transparent;
        padding: 0 4px;
    }
    QPushButton:hover {
        color: #8e5f40;
    }
    QPushButton:disabled {
        color: #c7b09c;
    }
    """
    _EMPTY_LABEL_QSS = "QLabel { color: #9a7b63; font-size: 12px; }"
    _HISTORY_ROW_QSS = """
    QWidget {
        background: transparent;
    }
    QWidget:hover {
        background: #efe2d3;
    }
    """
    _HISTORY_BUTTON_QSS = """
    QPushButton {
        text-align: left;
        color: #2f241c;
        font-size: 13px;
        border: none;
        background: transparent;
        padding: 0;
    }
    """
    _HOT_TAB_QSS = """
    QTabBar::tab {
        background: transparent;
        color: #866652;
        padding: 8px 12px;
        margin-right: 6px;
        border: none;
    }
    QTabBar::tab:selected {
        background: #ead2bb;
        color: #7f4d2a;
        font-weight: 600;
    }
    """
    _HOT_ROW_QSS = """
    QWidget {
        background: transparent;
    }
    QWidget:hover {
        background: #f2dfcc;
    }
    """
    _HOT_RANK_QSS = "QLabel { color: #b06b3c; font-weight: 700; font-size: 12px; }"
    _HOT_BUTTON_QSS = """
    QPushButton {
        text-align: left;
        color: #2d2017;
        font-size: 14px;
        font-weight: 600;
        border: none;
        background: transparent;
        padding: 0;
    }
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.clear_history_button: QPushButton | None = None
        self.hot_source_tab_bar = QTabBar(self)
        self.hot_tab_bar = QTabBar(self)
        self._history_title_label: QLabel | None = None
        self._hot_title_label: QLabel | None = None
        self._history_item_buttons: dict[str, QPushButton] = {}
        self._history_item_rows: dict[str, QWidget] = {}
        self._hot_item_buttons: dict[str, QPushButton] = {}
        self._history_delete_buttons: dict[str, QPushButton] = {}
        self._hot_source_types: list[str] = []
        self._hot_tab_types: list[str] = []
        self._hot_item_texts: list[str] = []
        self._hot_rank_labels: list[QLabel] = []

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._container = QWidget(self)
        self._container.setObjectName("globalSearchPopupContainer")
        self._container.setStyleSheet(self._CONTAINER_QSS)
        self._container_layout = QHBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.setSpacing(0)
        self._main_layout.addWidget(self._container)

        self._history_panel = QWidget(self._container)
        self._history_layout = QVBoxLayout(self._history_panel)
        self._history_layout.setContentsMargins(0, 0, 0, 0)
        self._history_layout.setSpacing(0)
        self._container_layout.addWidget(self._history_panel, 1)

        separator = QFrame(self._container)
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setStyleSheet(self._DIVIDER_QSS)
        self._container_layout.addWidget(separator)

        self._hot_panel = QWidget(self._container)
        self._hot_layout = QVBoxLayout(self._hot_panel)
        self._hot_layout.setContentsMargins(0, 0, 0, 0)
        self._hot_layout.setSpacing(0)
        self._container_layout.addWidget(self._hot_panel, 1)

        self._build_history_panel()
        self._build_hot_panel()

    def history_item_texts(self) -> list[str]:
        return [button.text() for button in self._history_item_buttons.values()]

    def hot_item_texts(self) -> list[str]:
        return list(self._hot_item_texts)

    def hot_source_titles(self) -> list[str]:
        return [self.hot_source_tab_bar.tabText(index) for index in range(self.hot_source_tab_bar.count())]

    def hot_tab_titles(self) -> list[str]:
        return [self.hot_tab_bar.tabText(index) for index in range(self.hot_tab_bar.count())]

    def current_hot_source(self) -> str:
        index = self.hot_source_tab_bar.currentIndex()
        if index < 0 or index >= len(self._hot_source_types):
            return ""
        return self._hot_source_types[index]

    def current_hot_tab_type(self) -> str:
        index = self.hot_tab_bar.currentIndex()
        if index < 0 or index >= len(self._hot_tab_types):
            return ""
        return self._hot_tab_types[index]

    def history_item_button(self, text: str) -> QPushButton:
        return self._history_item_buttons[text]

    def hot_item_button(self, text: str) -> QPushButton:
        return self._hot_item_buttons[text]

    def history_delete_button(self, keyword: str) -> QPushButton:
        return self._history_delete_buttons[keyword]

    def history_item_row(self, text: str) -> QWidget:
        return self._history_item_rows[text]

    def hot_item_ranks(self) -> list[str]:
        return [label.text() for label in self._hot_rank_labels]

    def set_sections(
        self,
        history: list[str],
        hot_source: str,
        hot_sources: list[tuple[str, str]],
        hot_type: str,
        hot_categories: list[tuple[str, str]],
        hotkeys: list[dict[str, str]],
    ) -> None:
        self._set_history_items(history)
        self._set_hot_sources(hot_sources, hot_source)
        self._set_hot_categories(hot_categories, hot_type)
        self._set_hot_items(hotkeys)
        self.adjustSize()

    def _build_history_panel(self) -> None:
        title_widget = QWidget(self._history_panel)
        title_layout = QHBoxLayout(title_widget)
        title_layout.setContentsMargins(16, 14, 16, 10)
        title = QLabel("搜索历史", title_widget)
        title.setStyleSheet(self._SECTION_TITLE_QSS)
        self._history_title_label = title
        title_layout.addWidget(title)
        title_layout.addStretch(1)
        clear_button = QPushButton("清空", title_widget)
        clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_button.setFlat(True)
        clear_button.setStyleSheet(self._ACTION_BUTTON_QSS)
        clear_button.clicked.connect(self._on_clear_clicked)
        title_layout.addWidget(clear_button)
        self.clear_history_button = clear_button
        self._history_layout.addWidget(title_widget)
        self._history_items_widget = QWidget(self._history_panel)
        self._history_items_layout = QVBoxLayout(self._history_items_widget)
        self._history_items_layout.setContentsMargins(8, 0, 8, 12)
        self._history_items_layout.setSpacing(0)
        self._history_layout.addWidget(self._history_items_widget, 1)

    def _build_hot_panel(self) -> None:
        title = QLabel("热搜", self._hot_panel)
        title.setContentsMargins(16, 14, 16, 10)
        title.setStyleSheet(self._SECTION_TITLE_QSS)
        self._hot_title_label = title
        self._hot_layout.addWidget(title)
        self.hot_source_tab_bar.setDocumentMode(True)
        self.hot_source_tab_bar.setExpanding(False)
        self.hot_source_tab_bar.setUsesScrollButtons(True)
        self.hot_source_tab_bar.setDrawBase(False)
        self.hot_source_tab_bar.setElideMode(Qt.TextElideMode.ElideNone)
        self.hot_source_tab_bar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hot_source_tab_bar.setStyleSheet(self._HOT_TAB_QSS)
        self.hot_source_tab_bar.currentChanged.connect(self._handle_hot_source_tab_changed)
        self._hot_layout.addWidget(self.hot_source_tab_bar)
        self.hot_tab_bar.setDocumentMode(True)
        self.hot_tab_bar.setExpanding(False)
        self.hot_tab_bar.setUsesScrollButtons(True)
        self.hot_tab_bar.setDrawBase(False)
        self.hot_tab_bar.setElideMode(Qt.TextElideMode.ElideNone)
        self.hot_tab_bar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hot_tab_bar.setStyleSheet(self._HOT_TAB_QSS)
        self.hot_tab_bar.currentChanged.connect(self._handle_hot_tab_changed)
        self._hot_layout.addWidget(self.hot_tab_bar)
        self._hot_items_widget = QWidget(self._hot_panel)
        self._hot_items_layout = QVBoxLayout(self._hot_items_widget)
        self._hot_items_layout.setContentsMargins(8, 10, 8, 12)
        self._hot_items_layout.setSpacing(0)
        self._hot_layout.addWidget(self._hot_items_widget, 1)

    def _set_history_items(self, history: list[str]) -> None:
        self._clear_layout(self._history_items_layout)
        self._history_item_buttons = {}
        self._history_item_rows = {}
        self._history_delete_buttons = {}
        history_exists = False
        for keyword in history:
            normalized = keyword.strip()
            if not normalized:
                continue
            history_exists = True
            self._add_history_item(normalized)
        if not history_exists:
            empty_label = QLabel("暂无搜索历史", self._history_items_widget)
            empty_label.setContentsMargins(8, 12, 8, 10)
            empty_label.setStyleSheet(self._EMPTY_LABEL_QSS)
            self._history_items_layout.addWidget(empty_label)
        if self.clear_history_button is not None:
            self.clear_history_button.setEnabled(history_exists)

    def _set_hot_sources(self, hot_sources: list[tuple[str, str]], hot_source: str) -> None:
        self._hot_source_types = [source for source, _ in hot_sources]
        self.hot_source_tab_bar.blockSignals(True)
        while self.hot_source_tab_bar.count() > 0:
            self.hot_source_tab_bar.removeTab(self.hot_source_tab_bar.count() - 1)
        for _, title_text in hot_sources:
            self.hot_source_tab_bar.addTab(title_text)
        try:
            index = self._hot_source_types.index(hot_source)
        except ValueError:
            index = 0 if self._hot_source_types else -1
        self.hot_source_tab_bar.setCurrentIndex(index)
        self.hot_source_tab_bar.blockSignals(False)

    def _set_hot_categories(self, hot_categories: list[tuple[str, str]], hot_type: str) -> None:
        self._hot_tab_types = [category for category, _ in hot_categories]
        self.hot_tab_bar.blockSignals(True)
        while self.hot_tab_bar.count() > 0:
            self.hot_tab_bar.removeTab(self.hot_tab_bar.count() - 1)
        for _, title_text in hot_categories:
            self.hot_tab_bar.addTab(title_text)
        try:
            index = self._hot_tab_types.index(hot_type)
        except ValueError:
            index = 0 if self._hot_tab_types else -1
        self.hot_tab_bar.setCurrentIndex(index)
        self.hot_tab_bar.blockSignals(False)

    def _set_hot_items(self, hotkeys: list[dict[str, str]]) -> None:
        self._clear_layout(self._hot_items_layout)
        self._hot_item_buttons = {}
        self._hot_item_texts = []
        self._hot_rank_labels = []
        hotkey_exists = False
        for index, item in enumerate(hotkeys[:10], start=1):
            title = str(item.get("title") or "").strip()
            query = str(item.get("query") or item.get("title") or "").strip()
            if not title or not query:
                continue
            hotkey_exists = True
            self._add_hot_item(index, title, query)
        if not hotkey_exists:
            empty_label = QLabel("暂无热搜词", self._hot_items_widget)
            empty_label.setContentsMargins(8, 12, 8, 10)
            empty_label.setStyleSheet(self._EMPTY_LABEL_QSS)
            self._hot_items_layout.addWidget(empty_label)

    def _add_history_item(self, keyword: str) -> None:
        row = QWidget(self._history_items_widget)
        row.setFixedHeight(self.HISTORY_ITEM_HEIGHT)
        row.setStyleSheet(self._HISTORY_ROW_QSS)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(12, 4, 10, 4)
        row_layout.setSpacing(8)
        item_button = QPushButton(keyword, row)
        item_button.setFixedHeight(self.HISTORY_ITEM_HEIGHT - 8)
        item_button.setCursor(Qt.CursorShape.PointingHandCursor)
        item_button.setFlat(True)
        item_button.setStyleSheet(self._HISTORY_BUTTON_QSS)
        item_button.clicked.connect(lambda checked=False, current_keyword=keyword: self._on_item_clicked(current_keyword))
        delete_button = QPushButton("删除", row)
        delete_button.setFixedHeight(self.HISTORY_ITEM_HEIGHT - 8)
        delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_button.setFlat(True)
        delete_button.setStyleSheet(self._ACTION_BUTTON_QSS)
        delete_button.clicked.connect(lambda checked=False, current_keyword=keyword: self._on_delete_clicked(current_keyword))
        row_layout.addWidget(item_button, 1)
        row_layout.addWidget(delete_button)
        self._history_item_buttons[keyword] = item_button
        self._history_item_rows[keyword] = row
        self._history_delete_buttons[keyword] = delete_button
        self._history_items_layout.addWidget(row)

    def _add_hot_item(self, index: int, text: str, query: str) -> None:
        row = QWidget(self._hot_items_widget)
        row.setFixedHeight(self.HOT_ITEM_HEIGHT)
        row.setStyleSheet(self._HOT_ROW_QSS)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(12, 6, 12, 6)
        row_layout.setSpacing(10)

        rank_label = QLabel(f"{index:02d}", row)
        rank_label.setStyleSheet(self._HOT_RANK_QSS)

        button = QPushButton(text, row)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFlat(True)
        button.setStyleSheet(self._HOT_BUTTON_QSS)
        button.clicked.connect(lambda checked=False, current_query=query: self._on_item_clicked(current_query))

        row_layout.addWidget(rank_label)
        row_layout.addWidget(button, 1)

        self._hot_rank_labels.append(rank_label)
        self._hot_item_buttons[text] = button
        self._hot_item_texts.append(text)
        self._hot_items_layout.addWidget(row)

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _handle_hot_tab_changed(self, index: int) -> None:
        if 0 <= index < len(self._hot_tab_types):
            self.hot_tab_changed.emit(self._hot_tab_types[index])

    def _handle_hot_source_tab_changed(self, index: int) -> None:
        if 0 <= index < len(self._hot_source_types):
            self.hot_source_changed.emit(self._hot_source_types[index])

    def _on_item_clicked(self, query: str) -> None:
        self.hide()
        self.item_clicked.emit(query)

    def _on_clear_clicked(self) -> None:
        self.hide()
        self.clear_history_requested.emit()

    def _on_delete_clicked(self, keyword: str) -> None:
        self.delete_history_requested.emit(keyword)

    def show_at(self, global_pos: QPoint, width: int) -> None:
        popup_width = max(width + 320, 720)
        self.setMinimumWidth(popup_width)
        self.setMaximumWidth(popup_width)
        self.move(global_pos)
        self.show()
        self.raise_()


@dataclass(slots=True)
class _MediaLoadResult:
    page: PosterGridPage
    items: list[Any]
    total: int
    empty_message: str
    push_breadcrumb: tuple[str, str] | None = None
    trim_breadcrumbs_to: int | None = None


@dataclass(slots=True)
class _TabDefinition:
    key: str
    title: str
    page: QWidget
    search_controller: Any | None = None
    global_search_only: bool = False


@dataclass(slots=True)
class _GlobalSearchResult:
    key: str
    title: str
    page: PosterGridPage
    items: list[Any]
    total: int
    page_number: int


class _NavigationTabs(QWidget):
    currentChanged = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.tab_bar = QTabBar(self)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setMovable(False)
        self.tab_bar.setDocumentMode(True)
        self.tab_bar.setUsesScrollButtons(False)
        self.plugin_overflow_button = QPushButton("更多", self)
        self.plugin_overflow_button.hide()
        self.content_stack = QStackedWidget(self)
        self._visible_widgets: list[QWidget] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        nav_row = QHBoxLayout()
        nav_row.setContentsMargins(0, 0, 0, 0)
        nav_row.setSpacing(8)
        nav_row.addWidget(self.tab_bar, 1)
        nav_row.addWidget(self.plugin_overflow_button, 0)
        layout.addLayout(nav_row)
        layout.addWidget(self.content_stack, 1)
        self.tab_bar.currentChanged.connect(self._handle_tab_bar_changed)

    def _handle_tab_bar_changed(self, index: int) -> None:
        widget = self.widget(index)
        if widget is None:
            return
        self.content_stack.setCurrentWidget(widget)
        if not self.signalsBlocked():
            self.currentChanged.emit(index)

    def clear(self) -> None:
        while self.tab_bar.count() > 0:
            self.tab_bar.removeTab(self.tab_bar.count() - 1)
        self._visible_widgets = []

    def ensure_widget(self, widget: QWidget) -> None:
        if self.content_stack.indexOf(widget) < 0:
            self.content_stack.addWidget(widget)

    def addTab(self, widget: QWidget, title: str) -> int:
        self.ensure_widget(widget)
        self._visible_widgets.append(widget)
        return self.tab_bar.addTab(title)

    def count(self) -> int:
        return self.tab_bar.count()

    def tabText(self, index: int) -> str:
        return self.tab_bar.tabText(index)

    def currentWidget(self) -> QWidget | None:
        return self.content_stack.currentWidget()

    def currentIndex(self) -> int:
        current_widget = self.currentWidget()
        if current_widget is None:
            return -1
        return self.indexOf(current_widget)

    def widget(self, index: int) -> QWidget | None:
        if 0 <= index < len(self._visible_widgets):
            return self._visible_widgets[index]
        return None

    def indexOf(self, widget: QWidget) -> int:
        try:
            return self._visible_widgets.index(widget)
        except ValueError:
            return -1

    def setCurrentIndex(self, index: int) -> None:
        widget = self.widget(index)
        if widget is None:
            return
        self.tab_bar.setCurrentIndex(index)
        self.content_stack.setCurrentWidget(widget)

    def setCurrentWidget(self, widget: QWidget) -> None:
        self.ensure_widget(widget)
        index = self.indexOf(widget)
        if index >= 0:
            self.tab_bar.setCurrentIndex(index)
            self.content_stack.setCurrentWidget(widget)
            return
        if self.tab_bar.currentIndex() != -1:
            self.tab_bar.setCurrentIndex(-1)
        self.content_stack.setCurrentWidget(widget)
        if not self.signalsBlocked():
            self.currentChanged.emit(-1)

    def blockSignals(self, block: bool) -> bool:
        previous = super().blockSignals(block)
        self.tab_bar.blockSignals(block)
        return previous

    def minimumSizeHint(self) -> QSize:
        content_hint = self.content_stack.minimumSizeHint()
        nav_height = max(self.tab_bar.minimumSizeHint().height(), self.plugin_overflow_button.minimumSizeHint().height())
        spacing = self.layout().spacing() if self.layout() is not None else 0
        return QSize(0, nav_height + spacing + content_hint.height())

    def sizeHint(self) -> QSize:
        content_hint = self.content_stack.sizeHint()
        nav_height = max(self.tab_bar.sizeHint().height(), self.plugin_overflow_button.sizeHint().height())
        spacing = self.layout().spacing() if self.layout() is not None else 0
        return QSize(0, nav_height + spacing + content_hint.height())


class MainWindow(QMainWindow, AsyncGuardMixin):
    logout_requested = Signal()
    _SEARCH_ICON_PATH = Path(__file__).resolve().parent.parent / "icons" / "search.svg"
    _SEARCH_POPUP_ICON_PATH = Path(__file__).resolve().parent.parent / "icons" / "rank.svg"

    def __init__(
            self,
            browse_controller,
            history_controller,
            player_controller,
            config,
            save_config=None,
            douban_controller=None,
            telegram_controller=None,
            bilibili_controller=None,
            live_controller=None,
            live_source_manager=None,
            emby_controller=None,
            jellyfin_controller=None,
            feiniu_controller=None,
            pansou_controller=None,
            spider_plugins=None,
            plugin_loader_task=None,
            plugin_manager=None,
            drive_detail_loader=None,
            offline_download_detail_loader=None,
            direct_parse_detail_loader=None,
            direct_parse_danmaku_loader=None,
            direct_parse_playback_history_loader=None,
            direct_parse_playback_history_saver=None,
            default_video_cover_loader=None,
            global_search_hotkey_loader=None,
            global_search_suggestion_loader=None,
            show_bilibili_tab: bool = False,
            show_emby_tab: bool = True,
            show_jellyfin_tab: bool = True,
            show_feiniu_tab: bool = True,
            m3u8_ad_filter=None,
            playback_parser_service=None,
            yt_dlp_service=None,
            metadata_hydrator_factory=None,
            metadata_scrape_service_factory=None,
            episode_title_enhancer_factory=None,
            metadata_binding_repository=None,
    ) -> None:
        super().__init__()
        self._init_async_guard()
        self._save_config = save_config or (lambda: None)
        self._m3u8_ad_filter = m3u8_ad_filter
        self._playback_parser_service = playback_parser_service
        self._yt_dlp_service = yt_dlp_service
        self._metadata_hydrator_factory = metadata_hydrator_factory
        self._metadata_scrape_service_factory = metadata_scrape_service_factory
        self._episode_title_enhancer_factory = episode_title_enhancer_factory
        self._metadata_binding_repository = metadata_binding_repository
        self._plugin_definitions = list(spider_plugins or [])
        self._plugin_loader_task = plugin_loader_task
        self._plugin_manager = plugin_manager
        self._drive_detail_loader = drive_detail_loader
        self._offline_download_detail_loader = offline_download_detail_loader
        self._direct_parse_detail_loader = direct_parse_detail_loader
        self._direct_parse_danmaku_loader = direct_parse_danmaku_loader
        self._direct_parse_playback_history_loader = direct_parse_playback_history_loader
        self._direct_parse_playback_history_saver = direct_parse_playback_history_saver
        self._default_video_cover_loader = default_video_cover_loader
        self._global_search_hotkey_loader = global_search_hotkey_loader or load_global_search_hotkey_payload
        self._global_search_suggestion_loader = global_search_suggestion_loader or load_360_search_suggestions
        self._live_source_manager = live_source_manager
        self._plugin_pages: list[tuple[PosterGridPage, _PluginController, str]] = []
        self._static_tab_definitions: list[_TabDefinition] = []
        self._trailing_tab_definitions: list[_TabDefinition] = []
        self._plugin_tab_definitions: list[_TabDefinition] = []
        self._hidden_plugin_tab_definitions: list[_TabDefinition] = []
        self._startup_plugin_load_started = False
        self._startup_plugin_load_request_id = 0
        self._startup_plugin_load_state = "loading" if callable(plugin_loader_task) else "idle"
        self._startup_plugin_load_error = ""
        self._startup_selected_category_id = str(getattr(config, "last_selected_category_id", "") or "").strip()
        selected_tab = str(getattr(config, "last_selected_tab", "") or "")
        self._startup_plugin_pending_tab_restore_key = (
            selected_tab if callable(plugin_loader_task) and selected_tab.startswith("plugin:") else ""
        )
        self._startup_plugin_pending_player_restore = False
        self._active_widget: QWidget | None = None
        self._plugin_actions = PluginActions(plugin_manager) if plugin_manager is not None else None
        self.nav_tabs = _NavigationTabs()
        self.nav_tabs.tab_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.plugin_overflow_button = self.nav_tabs.plugin_overflow_button
        self._tab_width_measure_bar = QTabBar(self)
        self._tab_width_measure_bar.setDocumentMode(True)
        self._tab_width_measure_bar.hide()
        self._plugin_overflow_drawer = PluginTabDrawer(self.nav_tabs)
        self._plugin_overflow_drawer.hide()
        self._startup_plugin_placeholder_page = QWidget(self)
        self.global_search_container = QWidget()
        self.global_search_edit = SearchInputWithHotkey()
        self.global_search_button = QPushButton("搜索")
        self.global_search_popup_button = QPushButton("")
        self.global_search_clear_button = QPushButton("清空")
        self.global_search_status_label = QLabel("")
        self._global_search_popup = GlobalSearchPopup()
        self.startup_plugin_status_label = QLabel("")
        self.startup_plugin_retry_button = QPushButton("重试加载插件")
        self.startup_plugin_retry_button.hide()
        self.plugin_manager_button = QPushButton("插件管理")
        self.live_source_manager_button = QPushButton("直播源管理")
        self.advanced_settings_button = QPushButton("高级设置")
        self.logout_button = QPushButton("退出登录")
        self.browse_page = BrowsePage(browse_controller, config=config, save_config=self._save_config)
        self.douban_page = PosterGridPage(
            douban_controller or _EmptyDoubanController(),
            initial_category_id=self._initial_category_id_for_tab("douban"),
        )
        self.telegram_page = PosterGridPage(
            telegram_controller or _EmptyTelegramController(),
            click_action="open",
            search_enabled=True,
            initial_category_id=self._initial_category_id_for_tab("telegram"),
        )
        self.bilibili_page = None
        if show_bilibili_tab:
            self.bilibili_page = PosterGridPage(
                bilibili_controller or _EmptyBilibiliController(),
                click_action="open",
                search_enabled=True,
                folder_navigation_enabled=True,
                initial_category_id=self._initial_category_id_for_tab("bilibili"),
            )
        self.live_page = PosterGridPage(
            live_controller or _EmptyLiveController(),
            click_action="open",
            folder_navigation_enabled=True,
            initial_category_id=self._initial_category_id_for_tab("live"),
        )
        self.emby_page = None
        if show_emby_tab:
            self.emby_page = PosterGridPage(
                emby_controller or _EmptyEmbyController(),
                click_action="open",
                search_enabled=True,
                folder_navigation_enabled=True,
                initial_category_id=self._initial_category_id_for_tab("emby"),
            )
        self.jellyfin_page = None
        if show_jellyfin_tab:
            self.jellyfin_page = PosterGridPage(
                jellyfin_controller or _EmptyJellyfinController(),
                click_action="open",
                search_enabled=True,
                folder_navigation_enabled=True,
                initial_category_id=self._initial_category_id_for_tab("jellyfin"),
            )
        self.feiniu_page = None
        if show_feiniu_tab:
            self.feiniu_page = PosterGridPage(
                feiniu_controller or _EmptyFeiniuController(),
                click_action="open",
                search_enabled=True,
                folder_navigation_enabled=True,
                initial_category_id=self._initial_category_id_for_tab("feiniu"),
            )
        self.history_page = HistoryPage(history_controller)
        self.pansou_page = None
        if pansou_controller is not None:
            self.pansou_page = PosterGridPage(
                pansou_controller,
                click_action="open",
                initial_category_id=self._initial_category_id_for_tab("pansou"),
            )
        self.browse_controller = browse_controller
        self.telegram_controller = telegram_controller or _EmptyTelegramController()
        self.bilibili_controller = bilibili_controller or _EmptyBilibiliController()
        self.live_controller = live_controller or _EmptyLiveController()
        self.emby_controller = emby_controller or _EmptyEmbyController()
        self.jellyfin_controller = jellyfin_controller or _EmptyJellyfinController()
        self.feiniu_controller = feiniu_controller or _EmptyFeiniuController()
        self.pansou_controller = pansou_controller
        self.player_controller = player_controller
        self.player_window: PlayerWindow | None = None
        self.help_dialog: ShortcutHelpDialog | None = None
        self.config = config
        self._open_request_id = 0
        self._media_request_id = 0
        self._restore_request_id = 0
        self._player_session_request_id = 0
        self._main_window_was_maximized_before_player = False
        self._open_request_signals = _AsyncRequestSignals()
        self._connect_async_signal(self._open_request_signals.succeeded, self._handle_open_request_succeeded)
        self._connect_async_signal(self._open_request_signals.failed, self._handle_open_request_failed)
        self._plugin_open_request_id = 0
        self._plugin_open_request_signals = _AsyncRequestSignals()
        self._connect_async_signal(
            self._plugin_open_request_signals.succeeded,
            self._handle_plugin_open_request_succeeded,
        )
        self._connect_async_signal(
            self._plugin_open_request_signals.failed,
            self._handle_plugin_open_request_failed,
        )
        self._pansou_resolve_request_id = 0
        self._pansou_resolve_request_signals = _AsyncRequestSignals()
        self._connect_async_signal(
            self._pansou_resolve_request_signals.succeeded,
            self._handle_pansou_resolve_succeeded,
        )
        self._connect_async_signal(
            self._pansou_resolve_request_signals.failed,
            self._handle_pansou_resolve_failed,
        )
        self._media_request_signals = _AsyncRequestSignals()
        self._connect_async_signal(self._media_request_signals.succeeded, self._handle_media_load_succeeded)
        self._connect_async_signal(self._media_request_signals.failed, self._handle_media_load_failed)
        self._restore_signals = _RestoreSignals()
        self._connect_async_signal(self._restore_signals.succeeded, self._handle_restore_succeeded)
        self._connect_async_signal(self._restore_signals.failed, self._handle_restore_failed)
        self._session_open_signals = _SessionOpenSignals()
        self._connect_async_signal(self._session_open_signals.succeeded, self._handle_session_open_succeeded)
        self._connect_async_signal(self._session_open_signals.failed, self._handle_session_open_failed)
        self._global_search_signals = _GlobalSearchSignals()
        self._connect_async_signal(self._global_search_signals.succeeded, self._handle_global_search_succeeded)
        self._connect_async_signal(self._global_search_signals.failed, self._handle_global_search_failed)
        self._startup_plugin_load_signals = _StartupPluginLoadSignals()
        self._connect_async_signal(
            self._startup_plugin_load_signals.loaded,
            self._handle_startup_plugin_loaded,
        )
        self._connect_async_signal(
            self._startup_plugin_load_signals.finished,
            self._handle_startup_plugin_load_finished,
        )
        self._connect_async_signal(
            self._startup_plugin_load_signals.failed,
            self._handle_startup_plugin_load_failed,
        )
        self._global_search_request_id = 0
        self._global_search_pending_keys: set[str] = set()
        self._global_search_results: dict[str, _GlobalSearchResult] = {}
        self._global_search_active = False
        self._global_search_in_progress = False
        self._global_search_keyword = ""
        self._global_search_popup_signals = _GlobalSearchPopupSignals()
        self._connect_async_signal(self._global_search_popup_signals.hotkeys_loaded, self._handle_global_search_hotkeys_loaded)
        self._global_search_hotkey_request_id = 0
        self._global_search_hotkey_cache: dict[tuple[str, str], list[dict[str, str]]] = {}
        self._global_search_hot_categories_by_source = {
            source: list(categories)
            for source, categories in _GLOBAL_SEARCH_HOT_SOURCE_CATEGORIES.items()
        }
        self._global_search_hotkey_active_source = self._normalize_global_search_hot_source(
            getattr(config, "global_search_hot_source", _DEFAULT_GLOBAL_SEARCH_HOT_SOURCE)
        )
        self._global_search_hotkey_preferred_type = _DEFAULT_GLOBAL_SEARCH_HOT_TYPE
        self._global_search_hotkey_active_type = self._fallback_global_search_hot_category(
            self._global_search_hotkey_active_source,
            self._global_search_hotkey_preferred_type,
        )
        self._app_event_filter_installed = False
        self._app_state_signal_connected = False

        self.global_search_container.setFixedWidth(400)
        self.global_search_edit.setPlaceholderText("搜索")
        self.global_search_edit.setClearButtonEnabled(True)
        self.global_search_edit.setStyleSheet(
            """
            QLineEdit {
                min-height: 36px;
                padding: 0 12px;
                border: 1px solid #d0d7de;
                border-radius: 18px;
                background: #ffffff;
            }
            QLineEdit:focus {
                border: 1px solid #409eff;
            }
            """
        )
        self.global_search_button.setText("")
        self.global_search_button.setIcon(load_icon(self._SEARCH_ICON_PATH))
        self.global_search_button.setFixedSize(36, 36)
        self.global_search_button.setStyleSheet(
            """
            QPushButton {
                border: 1px solid #d0d7de;
                border-radius: 18px;
                background: #ffffff;
                padding: 0;
            }
            QPushButton:hover {
                background: #f3f4f6;
            }
            """
        )
        self.global_search_popup_button.setIcon(load_icon(self._SEARCH_POPUP_ICON_PATH))
        self.global_search_popup_button.setFixedSize(36, 36)
        self.global_search_popup_button.setStyleSheet(
            """
            QPushButton {
                border: 1px solid #d0d7de;
                border-radius: 18px;
                background: #ffffff;
                padding: 0;
            }
            QPushButton:hover {
                background: #f3f4f6;
            }
            """
        )
        self.global_search_status_label.setWordWrap(True)
        self.global_search_clear_button.setEnabled(False)
        self.global_search_clear_button.hide()

        self._static_tab_definitions = [
            _TabDefinition("douban", "豆瓣电影", self.douban_page),
            _TabDefinition("telegram", "电报影视", self.telegram_page, self.telegram_controller),
        ]
        if self.bilibili_page is not None:
            self._static_tab_definitions.append(
                _TabDefinition("bilibili", "B站", self.bilibili_page, self.bilibili_controller)
            )
        self._static_tab_definitions.append(_TabDefinition("live", "网络直播", self.live_page))
        if self.emby_page is not None:
            self._static_tab_definitions.append(_TabDefinition("emby", "Emby", self.emby_page, self.emby_controller))
        if self.jellyfin_page is not None:
            self._static_tab_definitions.append(
                _TabDefinition("jellyfin", "Jellyfin", self.jellyfin_page, self.jellyfin_controller)
            )
        if self.feiniu_page is not None:
            self._static_tab_definitions.append(
                _TabDefinition("feiniu", "飞牛影视", self.feiniu_page, self.feiniu_controller)
            )
        if self.pansou_page is not None and self.pansou_controller is not None:
            self._static_tab_definitions.append(
                _TabDefinition(
                    "pansou",
                    "盘搜",
                    self.pansou_page,
                    self.pansou_controller,
                    global_search_only=True,
                )
            )
        self._trailing_tab_definitions = [
            _TabDefinition("browse", "文件浏览", self.browse_page),
            _TabDefinition("history", "播放记录", self.history_page),
        ]
        self._rebuild_spider_plugin_tabs()
        self.logout_button.clicked.connect(self.logout_requested.emit)
        self.plugin_overflow_button.clicked.connect(self._toggle_plugin_overflow_drawer)
        self.nav_tabs.tab_bar.customContextMenuRequested.connect(self._handle_plugin_tab_context_menu_requested)
        self._plugin_overflow_drawer.plugin_selected.connect(self._handle_hidden_plugin_selected)
        self._plugin_overflow_drawer.plugin_context_requested.connect(self._open_plugin_context_menu)
        self._plugin_overflow_drawer.close_requested.connect(self._close_plugin_overflow_drawer)
        self.startup_plugin_retry_button.clicked.connect(self._retry_startup_plugin_load)
        self.plugin_manager_button.clicked.connect(self._open_plugin_manager)
        self.live_source_manager_button.clicked.connect(self._open_live_source_manager)
        self.advanced_settings_button.clicked.connect(self._open_advanced_settings)
        self.global_search_button.clicked.connect(self._start_global_search)
        self.global_search_popup_button.clicked.connect(self._toggle_global_search_popup)
        self.global_search_clear_button.clicked.connect(self._clear_global_search)
        self.global_search_edit.returnPressed.connect(self._start_global_search)
        self.global_search_edit.textChanged.connect(self._handle_global_search_text_changed)
        self.global_search_edit.escape_pressed.connect(self._hide_global_search_popup)
        self._global_search_popup.item_clicked.connect(self._handle_global_search_popup_item_clicked)
        self._global_search_popup.clear_history_requested.connect(self._clear_global_search_history)
        self._global_search_popup.delete_history_requested.connect(self._delete_global_search_history)
        self._global_search_popup.hot_source_changed.connect(self._handle_global_search_hot_source_changed)
        self._global_search_popup.hot_tab_changed.connect(self._handle_global_search_hot_tab_changed)
        search_layout = QHBoxLayout(self.global_search_container)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(8)
        search_layout.addWidget(self.global_search_edit, 1)
        search_layout.addWidget(self.global_search_button)
        search_layout.addWidget(self.global_search_popup_button)
        self.header_layout = QHBoxLayout()
        self.header_layout.addStretch(1)
        self.header_layout.addWidget(self.global_search_container)
        self.header_layout.addStretch(1)
        self.header_layout.addWidget(self.startup_plugin_status_label)
        self.header_layout.addWidget(self.startup_plugin_retry_button)
        self.header_layout.addWidget(self.plugin_manager_button)
        self.header_layout.addWidget(self.live_source_manager_button)
        self.header_layout.addWidget(self.advanced_settings_button)
        self.header_layout.addWidget(self.logout_button)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.addLayout(self.header_layout)
        container_layout.addWidget(self.global_search_status_label)
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
        if self.bilibili_page is not None:
            bilibili_page = self.bilibili_page
            bilibili_page.item_open_requested.connect(self._handle_bilibili_item_open_requested)
            bilibili_page.folder_breadcrumb_requested.connect(
                lambda node_id, kind, index, page=bilibili_page: self._handle_media_breadcrumb_requested(
                    page,
                    self.bilibili_controller,
                    node_id,
                    kind,
                    index,
                )
            )
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
            emby_page = self.emby_page
            emby_page.item_open_requested.connect(self._handle_emby_item_open_requested)
            emby_page.folder_breadcrumb_requested.connect(
                lambda node_id, kind, index, page=emby_page: self._handle_media_breadcrumb_requested(
                    page,
                    self.emby_controller,
                    node_id,
                    kind,
                    index,
                )
            )
        if self.jellyfin_page is not None:
            jellyfin_page = self.jellyfin_page
            jellyfin_page.item_open_requested.connect(self._handle_jellyfin_item_open_requested)
            jellyfin_page.folder_breadcrumb_requested.connect(
                lambda node_id, kind, index, page=jellyfin_page: self._handle_media_breadcrumb_requested(
                    page,
                    self.jellyfin_controller,
                    node_id,
                    kind,
                    index,
                )
            )
        if self.feiniu_page is not None:
            feiniu_page = self.feiniu_page
            feiniu_page.item_open_requested.connect(self._handle_feiniu_item_open_requested)
            feiniu_page.folder_breadcrumb_requested.connect(
                lambda node_id, kind, index, page=feiniu_page: self._handle_media_breadcrumb_requested(
                    page,
                    self.feiniu_controller,
                    node_id,
                    kind,
                    index,
                )
            )
        if self.pansou_page is not None:
            self.pansou_page.item_open_requested.connect(self._handle_pansou_item_open_requested)

        self.douban_page.unauthorized.connect(self.logout_requested.emit)
        self.douban_page.selected_category_changed.connect(
            lambda category_id, page=self.douban_page: self._handle_selected_category_changed(page, category_id)
        )
        self.telegram_page.unauthorized.connect(self.logout_requested.emit)
        self.telegram_page.selected_category_changed.connect(
            lambda category_id, page=self.telegram_page: self._handle_selected_category_changed(page, category_id)
        )
        if self.bilibili_page is not None:
            self.bilibili_page.unauthorized.connect(self.logout_requested.emit)
            self.bilibili_page.selected_category_changed.connect(
                lambda category_id, page=self.bilibili_page: self._handle_selected_category_changed(page, category_id)
            )
        self.live_page.unauthorized.connect(self.logout_requested.emit)
        self.live_page.selected_category_changed.connect(
            lambda category_id, page=self.live_page: self._handle_selected_category_changed(page, category_id)
        )
        if self.emby_page is not None:
            self.emby_page.unauthorized.connect(self.logout_requested.emit)
            self.emby_page.selected_category_changed.connect(
                lambda category_id, page=self.emby_page: self._handle_selected_category_changed(page, category_id)
            )
        if self.jellyfin_page is not None:
            self.jellyfin_page.unauthorized.connect(self.logout_requested.emit)
            self.jellyfin_page.selected_category_changed.connect(
                lambda category_id, page=self.jellyfin_page: self._handle_selected_category_changed(page, category_id)
            )
        if self.feiniu_page is not None:
            self.feiniu_page.unauthorized.connect(self.logout_requested.emit)
            self.feiniu_page.selected_category_changed.connect(
                lambda category_id, page=self.feiniu_page: self._handle_selected_category_changed(page, category_id)
            )
        if self.pansou_page is not None:
            self.pansou_page.unauthorized.connect(self.logout_requested.emit)
            self.pansou_page.selected_category_changed.connect(
                lambda category_id, page=self.pansou_page: self._handle_selected_category_changed(page, category_id)
            )
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

        self._refresh_navigation_tabs()
        self._sync_startup_plugin_loading_ui()
        self._sync_global_search_action_state()
        self._handle_tab_changed(self.nav_tabs.currentIndex())

    def show_browse_path(self, path: str) -> None:
        self.browse_page.load_path(path)
        self.nav_tabs.setCurrentWidget(self.browse_page)

    def _all_tab_definitions(self) -> list[_TabDefinition]:
        return [*self._static_tab_definitions, *self._plugin_tab_definitions, *self._trailing_tab_definitions]

    def _startup_plugin_placeholder_definition(self) -> _TabDefinition | None:
        if (
            self._global_search_active
            or self._startup_plugin_load_state == "idle"
            or self._plugin_tab_definitions
        ):
            return None
        title = "插件加载中" if self._startup_plugin_load_state == "loading" else "插件加载失败"
        return _TabDefinition("plugin:startup-placeholder", title, self._startup_plugin_placeholder_page)

    def _initial_category_id_for_tab(self, tab_key: str) -> str:
        del tab_key
        return self._startup_selected_category_id

    def _visible_tab_definitions(self) -> list[_TabDefinition]:
        if not self._global_search_active:
            return [
                definition
                for definition in self._all_tab_definitions()
                if not definition.global_search_only
            ]
        return [definition for definition in self._all_tab_definitions() if definition.key in self._global_search_results]

    def _global_search_title_overrides(self) -> dict[str, str]:
        return {
            key: f"{result.title}({result.total})"
            for key, result in self._global_search_results.items()
        }

    def _sync_startup_plugin_loading_ui(self) -> None:
        if self._startup_plugin_load_state == "failed":
            self.startup_plugin_status_label.setText(self._startup_plugin_load_error or "插件加载失败")
            self.startup_plugin_retry_button.show()
            return
        self.startup_plugin_status_label.setText("")
        self.startup_plugin_retry_button.hide()

    def _plugin_tab_title_width(self, title: str) -> int:
        self._tab_width_measure_bar.setFont(self.nav_tabs.tab_bar.font())
        index = self._tab_width_measure_bar.addTab(title)
        width = self._tab_width_measure_bar.tabSizeHint(index).width()
        self._tab_width_measure_bar.removeTab(index)
        return width

    def _plugin_overflow_button_width(self) -> int:
        return max(self.plugin_overflow_button.sizeHint().width(), 84)

    def _available_plugin_tab_width(self) -> int:
        tab_bar_width = self.nav_tabs.tab_bar.width()
        if tab_bar_width <= 0:
            tab_bar_width = max(
                self.nav_tabs.width() - self._plugin_overflow_button_width() - 8,
                0,
            )
        static_width = sum(
            self._plugin_tab_title_width(definition.title)
            for definition in self._static_tab_definitions
            if not definition.global_search_only
        )
        trailing_width = sum(
            self._plugin_tab_title_width(definition.title)
            for definition in self._trailing_tab_definitions
            if not definition.global_search_only
        )
        available = tab_bar_width - static_width - trailing_width
        return max(available, 0)

    def _split_visible_and_hidden_plugin_tabs(self) -> tuple[list[_TabDefinition], list[_TabDefinition]]:
        available = self._available_plugin_tab_width()
        if not self._plugin_tab_definitions:
            return [], []
        visible: list[_TabDefinition] = []
        hidden: list[_TabDefinition] = []
        used = 0
        for definition in self._plugin_tab_definitions:
            width = self._plugin_tab_title_width(definition.title)
            if used + width <= available:
                visible.append(definition)
                used += width
            else:
                hidden.append(definition)
        return visible, hidden

    def _refresh_navigation_tabs(self) -> None:
        current_widget = self._active_widget or self.nav_tabs.currentWidget()
        title_overrides = self._global_search_title_overrides() if self._global_search_active else {}
        for definition in self._all_tab_definitions():
            self.nav_tabs.ensure_widget(definition.page)
        if self._global_search_active:
            definitions = [definition for definition in self._all_tab_definitions() if definition.key in self._global_search_results]
            self._hidden_plugin_tab_definitions = []
            self.plugin_overflow_button.hide()
            self._close_plugin_overflow_drawer()
        else:
            visible_static_definitions = [
                definition for definition in self._static_tab_definitions if not definition.global_search_only
            ]
            visible_plugins, hidden_plugins = self._split_visible_and_hidden_plugin_tabs()
            placeholder_definition = self._startup_plugin_placeholder_definition()
            if placeholder_definition is not None:
                self._hidden_plugin_tab_definitions = []
                self.plugin_overflow_button.hide()
                self._close_plugin_overflow_drawer()
                definitions = [*visible_static_definitions, placeholder_definition, *self._trailing_tab_definitions]
            else:
                self._hidden_plugin_tab_definitions = hidden_plugins
                self.plugin_overflow_button.setVisible(bool(hidden_plugins))
                self.plugin_overflow_button.setText(f"更多({len(hidden_plugins)})" if hidden_plugins else "更多")
                if not hidden_plugins:
                    self._close_plugin_overflow_drawer()
                definitions = [*visible_static_definitions, *visible_plugins, *self._trailing_tab_definitions]

        self.nav_tabs.blockSignals(True)
        self.nav_tabs.clear()
        for definition in definitions:
            self.nav_tabs.addTab(definition.page, title_overrides.get(definition.key, definition.title))
        self.nav_tabs.blockSignals(False)

        if current_widget is not None:
            current_index = self.nav_tabs.indexOf(current_widget)
            if current_index >= 0:
                self.nav_tabs.setCurrentIndex(current_index)
                self._sync_plugin_overflow_drawer()
                return
            if not self._global_search_active and self._tab_key_for_widget(current_widget) is not None:
                self.nav_tabs.setCurrentWidget(current_widget)
                self._sync_plugin_overflow_drawer()
                return
        if not self._global_search_active and self._restore_saved_tab_selection(definitions):
            self._sync_plugin_overflow_drawer()
            return
        if self.nav_tabs.count() > 0:
            self.nav_tabs.setCurrentIndex(0)
        self._sync_plugin_overflow_drawer()

    def _refresh_visible_tabs(self) -> None:
        self._refresh_navigation_tabs()

    def _restore_saved_tab_selection(self, definitions: list[_TabDefinition]) -> bool:
        selected_key = getattr(self.config, "last_selected_tab", "douban") or "douban"
        for index, definition in enumerate(definitions):
            if definition.key == selected_key:
                self.nav_tabs.setCurrentIndex(index)
                return True
        return False

    def _tab_key_for_widget(self, widget: QWidget | None) -> str | None:
        if widget is None:
            return None
        for definition in self._all_tab_definitions():
            if definition.page is widget:
                return definition.key
        return None

    def _remember_selected_tab(self, widget: QWidget) -> None:
        selected_key = self._tab_key_for_widget(widget)
        if (
            self._startup_plugin_pending_tab_restore_key
            and self._startup_plugin_load_state == "loading"
            and selected_key != self._startup_plugin_pending_tab_restore_key
        ):
            return
        if selected_key is None or self.config.last_selected_tab == selected_key:
            return
        self.config.last_selected_tab = selected_key
        self._save_config()

    def _remember_selected_category(self, widget: QWidget, category_id: str) -> None:
        if not category_id:
            return
        selected_key = self._tab_key_for_widget(widget)
        if selected_key is None:
            return
        if (
            self.config.last_selected_category_tab == selected_key
            and self.config.last_selected_category_id == category_id
        ):
            return
        self.config.last_selected_category_tab = selected_key
        self.config.last_selected_category_id = category_id
        self._save_config()

    def _handle_selected_category_changed(self, page: PosterGridPage, category_id: str) -> None:
        if self._global_search_active or self.nav_tabs.currentWidget() is not page:
            return
        self._remember_selected_category(page, category_id)

    def _sync_global_search_action_state(self) -> None:
        has_keyword = bool(self.global_search_edit.text().strip())
        self.global_search_button.setEnabled(has_keyword)
        self.global_search_clear_button.setEnabled(self._global_search_active or has_keyword)

    def _global_search_history(self) -> list[str]:
        return [str(item or "").strip() for item in getattr(self.config, "global_search_history", []) if str(item or "").strip()]

    def _filtered_global_search_history(self, keyword: str) -> list[str]:
        history = self._global_search_history()
        normalized_keyword = keyword.strip()
        if not normalized_keyword:
            return history
        lowered_keyword = normalized_keyword.casefold()
        return [item for item in history if lowered_keyword in item.casefold()]

    def _normalize_global_search_hot_source(self, source: str) -> str:
        normalized = str(source or "").strip()
        return normalized if normalized in _GLOBAL_SEARCH_HOT_SOURCE_TITLES else _DEFAULT_GLOBAL_SEARCH_HOT_SOURCE

    def _global_search_hot_sources(self) -> list[tuple[str, str]]:
        return [
            (source, _GLOBAL_SEARCH_HOT_SOURCE_TITLES[source])
            for source in _GLOBAL_SEARCH_HOT_SOURCE_ORDER
        ]

    def _global_search_hot_categories(self, source: str) -> list[tuple[str, str]]:
        normalized_source = self._normalize_global_search_hot_source(source)
        return list(self._global_search_hot_categories_by_source.get(normalized_source, []))

    def _fallback_global_search_hot_category(self, source: str, preferred: str) -> str:
        categories = self._global_search_hot_categories(source)
        if not categories:
            return _DEFAULT_GLOBAL_SEARCH_HOT_TYPE
        normalized_preferred = str(preferred or "").strip()
        if normalized_preferred and any(category == normalized_preferred for category, _ in categories):
            return normalized_preferred
        return categories[0][0]

    def _set_global_search_hot_source(self, source: str) -> None:
        normalized_source = self._normalize_global_search_hot_source(source)
        self._global_search_hotkey_active_source = normalized_source
        self.config.global_search_hot_source = normalized_source

    def _hotkey_cache_key(self, source: str, hot_type: str) -> tuple[str, str]:
        return (self._normalize_global_search_hot_source(source), str(hot_type or "").strip())

    def _call_global_search_hotkey_loader(self, source: str, hot_type: str) -> object:
        loader = self._global_search_hotkey_loader
        try:
            signature = inspect.signature(loader)
        except (TypeError, ValueError):
            signature = None
        if signature is None:
            return loader(source, hot_type)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        has_varargs = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        )
        if has_varargs or len(positional) >= 2:
            return loader(source, hot_type)
        return loader(hot_type)

    def _normalize_global_search_hotkey_load_result(
        self,
        source: str,
        hot_type: str,
        payload: object,
    ) -> _GlobalSearchHotkeyLoadResult:
        if isinstance(payload, _GlobalSearchHotkeyLoadResult):
            categories = (
                list(payload.categories)
                if payload.categories is not None
                else None
            )
            return _GlobalSearchHotkeyLoadResult(
                source=self._normalize_global_search_hot_source(payload.source or source),
                category=str(payload.category or hot_type).strip() or hot_type,
                items=_normalize_hot_search_items(payload.items),
                categories=categories,
            )
        if isinstance(payload, dict):
            items = _normalize_hot_search_items(payload.get("items"))
            categories_value = payload.get("categories")
            categories = None
            if isinstance(categories_value, list):
                normalized_categories: list[tuple[str, str]] = []
                for item in categories_value:
                    if not isinstance(item, (list, tuple)) or len(item) < 2:
                        continue
                    category_key = str(item[0] or "").strip()
                    category_title = str(item[1] or "").strip()
                    if category_key and category_title:
                        normalized_categories.append((category_key, category_title))
                categories = normalized_categories or None
            return _GlobalSearchHotkeyLoadResult(
                source=self._normalize_global_search_hot_source(str(payload.get("source") or source)),
                category=str(payload.get("category") or hot_type).strip() or hot_type,
                items=items,
                categories=categories,
            )
        return _GlobalSearchHotkeyLoadResult(
            source=self._normalize_global_search_hot_source(source),
            category=hot_type,
            items=_normalize_hot_search_items(payload),
        )

    def _position_global_search_popup(self) -> None:
        if not self._global_search_popup.isVisible():
            return
        pos = self.global_search_container.mapToGlobal(QPoint(0, self.global_search_container.height() + 4))
        self._global_search_popup.show_at(pos, self.global_search_container.width())

    def _show_global_search_popup(self) -> None:
        self._render_global_search_popup()
        pos = self.global_search_container.mapToGlobal(QPoint(0, self.global_search_container.height() + 4))
        self._global_search_popup.show_at(pos, self.global_search_container.width())

    def _hide_global_search_popup(self) -> None:
        self._global_search_popup.hide()

    def _dismiss_global_search_popup(self) -> None:
        self._hide_global_search_popup()

    def _dismiss_visible_global_search_popup(self) -> None:
        if self._global_search_popup.isVisible():
            self._dismiss_global_search_popup()

    @staticmethod
    def _widget_contains_global_pos(widget: QWidget, global_pos: QPoint) -> bool:
        if widget is None or not widget.isVisible():
            return False
        local_pos = widget.mapFromGlobal(global_pos)
        return widget.rect().contains(local_pos)

    def _handle_global_search_global_mouse_press(self, global_pos: QPoint) -> None:
        if not self._global_search_popup.isVisible():
            return
        if (
            self._widget_contains_global_pos(self.global_search_edit, global_pos)
            or self._widget_contains_global_pos(self.global_search_popup_button, global_pos)
            or self._widget_contains_global_pos(self._global_search_popup, global_pos)
        ):
            return
        self._dismiss_global_search_popup()

    def _handle_application_state_changed(self, state) -> None:
        if state == Qt.ApplicationState.ApplicationInactive:
            self._dismiss_global_search_popup()

    def _record_global_search_history(self, keyword: str) -> None:
        normalized_keyword = keyword.strip()
        if not normalized_keyword:
            return
        previous = self._global_search_history()
        updated = [normalized_keyword, *[item for item in previous if item != normalized_keyword]][:10]
        if updated == previous:
            return
        self.config.global_search_history = updated
        self._save_config()

    def _clear_global_search_history(self) -> None:
        if not self._global_search_history():
            return
        self.config.global_search_history = []
        self._save_config()
        self._render_global_search_popup()

    def _delete_global_search_history(self, keyword: str) -> None:
        previous = self._global_search_history()
        updated = [item for item in previous if item != keyword]
        if updated == previous:
            return
        self.config.global_search_history = updated
        self._save_config()
        self._render_global_search_popup()

    def _handle_global_search_popup_item_clicked(self, keyword: str) -> None:
        self.global_search_edit.setText(keyword)
        self._hide_global_search_popup()
        self._start_global_search()

    def _toggle_global_search_popup(self) -> None:
        if self._global_search_popup.isVisible():
            self._hide_global_search_popup()
            return
        self._show_global_search_popup()
        cache_key = self._hotkey_cache_key(
            self._global_search_hotkey_active_source,
            self._global_search_hotkey_active_type,
        )
        if cache_key not in self._global_search_hotkey_cache:
            self._request_global_search_hotkeys(
                self._global_search_hotkey_active_source,
                self._global_search_hotkey_active_type,
            )

    def _handle_global_search_hot_tab_changed(self, hot_type: str) -> None:
        self._global_search_hotkey_preferred_type = hot_type
        self._global_search_hotkey_active_type = hot_type
        self._render_global_search_popup()
        cache_key = self._hotkey_cache_key(self._global_search_hotkey_active_source, hot_type)
        if cache_key not in self._global_search_hotkey_cache:
            self._request_global_search_hotkeys(self._global_search_hotkey_active_source, hot_type)

    def _handle_global_search_hot_source_changed(self, source: str) -> None:
        normalized_source = self._normalize_global_search_hot_source(source)
        if normalized_source == self._global_search_hotkey_active_source:
            return
        self._set_global_search_hot_source(normalized_source)
        self._global_search_hotkey_active_type = self._fallback_global_search_hot_category(
            normalized_source,
            self._global_search_hotkey_preferred_type,
        )
        self._save_config()
        self._render_global_search_popup()
        cache_key = self._hotkey_cache_key(
            self._global_search_hotkey_active_source,
            self._global_search_hotkey_active_type,
        )
        if cache_key not in self._global_search_hotkey_cache:
            self._request_global_search_hotkeys(
                self._global_search_hotkey_active_source,
                self._global_search_hotkey_active_type,
            )

    def _render_global_search_popup(self) -> None:
        hot_categories = self._global_search_hot_categories(self._global_search_hotkey_active_source)
        self._global_search_hotkey_active_type = self._fallback_global_search_hot_category(
            self._global_search_hotkey_active_source,
            self._global_search_hotkey_preferred_type,
        )
        hotkeys = self._global_search_hotkey_cache.get(
            self._hotkey_cache_key(
                self._global_search_hotkey_active_source,
                self._global_search_hotkey_active_type,
            ),
            [],
        )
        self._global_search_popup.set_sections(
            self._global_search_history(),
            self._global_search_hotkey_active_source,
            self._global_search_hot_sources(),
            self._global_search_hotkey_active_type,
            hot_categories,
            hotkeys,
        )

    def _request_global_search_hotkeys(self, source: str, hot_type: str) -> None:
        self._global_search_hotkey_request_id += 1
        request_id = self._global_search_hotkey_request_id

        def run() -> None:
            try:
                payload = self._call_global_search_hotkey_loader(source, hot_type)
            except Exception:
                payload = []
            if self._is_window_alive():
                result = self._normalize_global_search_hotkey_load_result(source, hot_type, payload)
                self._global_search_popup_signals.hotkeys_loaded.emit(request_id, result)

        threading.Thread(target=run, daemon=True).start()

    def _handle_global_search_hotkeys_loaded(self, request_id: int, result: _GlobalSearchHotkeyLoadResult) -> None:
        if request_id != self._global_search_hotkey_request_id:
            return
        normalized_source = self._normalize_global_search_hot_source(result.source)
        if result.categories is not None:
            self._global_search_hot_categories_by_source[normalized_source] = [
                (str(category or "").strip(), str(title or "").strip())
                for category, title in result.categories
                if str(category or "").strip() and str(title or "").strip()
            ] or list(_GLOBAL_SEARCH_HOT_SOURCE_CATEGORIES.get(normalized_source, []))
        active_category = str(result.category or "").strip() or self._fallback_global_search_hot_category(
            normalized_source,
            self._global_search_hotkey_preferred_type,
        )
        self._global_search_hotkey_cache[self._hotkey_cache_key(normalized_source, active_category)] = [
            {
                "title": str(item.get("title") or "").strip(),
                "query": str(item.get("query") or item.get("title") or "").strip(),
            }
            for item in result.items
            if str(item.get("title") or "").strip()
        ]
        if normalized_source == self._global_search_hotkey_active_source:
            next_active_type = self._fallback_global_search_hot_category(
                normalized_source,
                self._global_search_hotkey_preferred_type,
            )
            category_changed = next_active_type != self._global_search_hotkey_active_type
            self._global_search_hotkey_active_type = next_active_type
            if category_changed:
                cache_key = self._hotkey_cache_key(normalized_source, next_active_type)
                if cache_key not in self._global_search_hotkey_cache:
                    self._request_global_search_hotkeys(normalized_source, next_active_type)
            self._render_global_search_popup()

    def _handle_global_search_text_changed(self) -> None:
        if not self.global_search_edit.text().strip() and self._global_search_active:
            self._clear_global_search()
        self._sync_global_search_action_state()

    def _handle_tab_changed(self, index: int) -> None:
        self._dismiss_visible_global_search_popup()
        widget = self.nav_tabs.widget(index) if index >= 0 else self.nav_tabs.currentWidget()
        if widget is None:
            return
        self._active_widget = widget
        selected_key = self._tab_key_for_widget(widget)
        if (
            self._startup_plugin_pending_tab_restore_key
            and self._startup_plugin_load_started
            and self._startup_plugin_load_state == "loading"
            and widget is not self._startup_plugin_placeholder_page
            and selected_key != self._startup_plugin_pending_tab_restore_key
        ):
            self._startup_plugin_pending_tab_restore_key = ""
        self._sync_plugin_overflow_drawer()
        if self._global_search_active:
            return
        self._remember_selected_tab(widget)
        if isinstance(widget, PosterGridPage):
            self._remember_selected_category(widget, widget.selected_category_id)
        if widget is self.douban_page:
            self.douban_page.ensure_loaded()
            return
        if widget is self.telegram_page:
            self.telegram_page.ensure_loaded()
            return
        if widget is self.bilibili_page and self.bilibili_page is not None:
            self.bilibili_page.ensure_loaded()
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
        if widget is self.feiniu_page and self.feiniu_page is not None:
            self.feiniu_page.ensure_loaded()
            return
        for page, _controller, _plugin_id in self._plugin_pages:
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

    def _overflow_drawer_items(self) -> list[tuple[str, str, bool]]:
        active_key = self._tab_key_for_widget(self._active_widget or self.nav_tabs.currentWidget())
        return [
            (definition.key, definition.title, definition.key == active_key)
            for definition in self._hidden_plugin_tab_definitions
        ]

    @staticmethod
    def _normalize_plugin_id(plugin_key: str) -> str:
        return plugin_key.removeprefix("plugin:")

    def _plugin_row_by_id(self, plugin_id: str):
        if self._plugin_manager is None:
            return None
        normalized_plugin_id = self._normalize_plugin_id(plugin_id)
        list_plugins = getattr(self._plugin_manager, "list_plugins", None)
        if not callable(list_plugins):
            return None
        for plugin in list_plugins():
            if str(getattr(plugin, "id", "")) == normalized_plugin_id:
                return plugin
        return None

    def _plugin_id_for_visible_tab_index(self, index: int) -> str | None:
        widget = self.nav_tabs.widget(index)
        tab_key = self._tab_key_for_widget(widget)
        if tab_key is None or not tab_key.startswith("plugin:"):
            return None
        return tab_key.removeprefix("plugin:")

    def _plugin_toggle_action_text(self, plugin_id: str) -> str:
        plugin = self._plugin_row_by_id(plugin_id)
        if plugin is None:
            return "启用"
        return "禁用" if bool(getattr(plugin, "enabled", False)) else "启用"

    def _position_plugin_overflow_drawer(self) -> None:
        content_geometry = self.nav_tabs.content_stack.geometry()
        if content_geometry.width() <= 0 or content_geometry.height() <= 0:
            return
        drawer_width = min(self._plugin_overflow_drawer.width(), content_geometry.width())
        self._plugin_overflow_drawer.setGeometry(
            content_geometry.x() + content_geometry.width() - drawer_width,
            content_geometry.y(),
            drawer_width,
            content_geometry.height(),
        )

    def _sync_plugin_overflow_drawer(self, *, reset_search: bool = False) -> None:
        if not self._hidden_plugin_tab_definitions:
            self._close_plugin_overflow_drawer()
            return
        if reset_search:
            self._plugin_overflow_drawer.search_edit.clear()
        self._plugin_overflow_drawer.set_plugins(self._overflow_drawer_items())
        if self._plugin_overflow_drawer.isVisible():
            self._position_plugin_overflow_drawer()

    def _toggle_plugin_overflow_drawer(self) -> None:
        self._dismiss_visible_global_search_popup()
        if self._plugin_overflow_drawer.isVisible():
            self._close_plugin_overflow_drawer()
            return
        self._open_plugin_overflow_drawer()

    def _open_plugin_overflow_drawer(self) -> None:
        if not self._hidden_plugin_tab_definitions:
            return
        self._sync_plugin_overflow_drawer(reset_search=True)
        self._position_plugin_overflow_drawer()
        self._plugin_overflow_drawer.show()
        self._plugin_overflow_drawer.raise_()
        self._plugin_overflow_drawer.search_edit.setFocus()

    def _close_plugin_overflow_drawer(self) -> None:
        self._plugin_overflow_drawer.hide()

    def _handle_hidden_plugin_selected(self, plugin_key: str) -> None:
        definition = next((item for item in self._plugin_tab_definitions if item.key == plugin_key), None)
        if definition is None:
            return
        self._close_plugin_overflow_drawer()
        self.nav_tabs.setCurrentWidget(definition.page)

    def _handle_plugin_tab_context_menu_requested(self, pos: QPoint) -> None:
        index = self.nav_tabs.tab_bar.tabAt(pos)
        if index < 0:
            return
        plugin_id = self._plugin_id_for_visible_tab_index(index)
        if not plugin_id:
            return
        self._open_plugin_context_menu(plugin_id, self.nav_tabs.tab_bar.mapToGlobal(pos))

    def _open_plugin_context_menu(self, plugin_key: str, global_pos: QPoint) -> None:
        self._dismiss_visible_global_search_popup()
        plugin_id = self._normalize_plugin_id(plugin_key)
        if self._plugin_actions is None:
            return
        menu = QMenu(self)
        reload_action = menu.addAction("重新加载")
        rename_action = menu.addAction("编辑名称")
        config_action = menu.addAction("编辑配置")
        toggle_action = menu.addAction(self._plugin_toggle_action_text(plugin_id))
        chosen = menu.exec(global_pos)
        if chosen is reload_action:
            self._run_plugin_context_action("refresh", plugin_id)
        elif chosen is rename_action:
            self._run_plugin_context_action("rename", plugin_id)
        elif chosen is config_action:
            self._run_plugin_context_action("edit_config", plugin_id)
        elif chosen is toggle_action:
            self._run_plugin_context_action("toggle_enabled", plugin_id)

    def _run_plugin_context_action(self, action_name: str, plugin_id: str) -> bool:
        if self._plugin_actions is None:
            return False
        action_map = {
            "refresh": self._plugin_actions.refresh_plugin,
            "rename": self._plugin_actions.rename_plugin,
            "edit_config": self._plugin_actions.edit_plugin_config,
            "toggle_enabled": self._plugin_actions.toggle_plugin_enabled,
        }
        action = action_map.get(action_name)
        if action is None:
            return False
        result = action(self, int(self._normalize_plugin_id(plugin_id)))
        if not result.changed or result.plugin_id is None:
            return False
        self._reload_changed_plugin_tabs([str(result.plugin_id)])
        self._sync_plugin_overflow_drawer(reset_search=False)
        return True

    def _handle_douban_search_requested(self, keyword: str) -> None:
        self.global_search_edit.setText(keyword)
        self._start_global_search()

    def _start_startup_plugin_load(self) -> None:
        if self._startup_plugin_load_started or not callable(self._plugin_loader_task):
            return
        self._startup_plugin_load_started = True
        self._startup_plugin_load_request_id += 1
        request_id = self._startup_plugin_load_request_id

        def run() -> None:
            try:
                plugins = self._plugin_loader_task()
                if plugins is None:
                    plugins = []
                for plugin in plugins:
                    if self._is_window_alive():
                        self._startup_plugin_load_signals.loaded.emit(request_id, plugin)
                if self._is_window_alive():
                    self._startup_plugin_load_signals.finished.emit(request_id)
            except Exception as exc:
                if self._is_window_alive():
                    self._startup_plugin_load_signals.failed.emit(request_id, str(exc))

        threading.Thread(target=run, daemon=True).start()

    def _handle_startup_plugin_loaded(self, request_id: int, plugin) -> None:
        if request_id != self._startup_plugin_load_request_id:
            return
        self._append_plugin_definition(plugin)
        self._refresh_visible_tabs()
        if self._startup_plugin_pending_tab_restore_key:
            for page, _controller, plugin_id in self._plugin_pages:
                if f"plugin:{plugin_id}" == self._startup_plugin_pending_tab_restore_key:
                    self.nav_tabs.setCurrentWidget(page)
                    break
        if (
            self._startup_plugin_pending_player_restore
            and self.config.last_playback_source == "plugin"
            and self.config.last_playback_source_key
            and self._plugin_controller_by_id(self.config.last_playback_source_key) is not None
        ):
            self._startup_plugin_pending_player_restore = False
            self._start_restore_last_player()

    def _handle_startup_plugin_load_finished(self, request_id: int) -> None:
        if request_id != self._startup_plugin_load_request_id:
            return
        self._startup_plugin_load_state = "idle"
        self._startup_plugin_load_error = ""
        self._sync_startup_plugin_loading_ui()
        self._startup_plugin_pending_tab_restore_key = ""
        if self._startup_plugin_pending_player_restore:
            self._startup_plugin_pending_player_restore = False
            self._start_restore_last_player()
        self._refresh_visible_tabs()

    def _handle_startup_plugin_load_failed(self, request_id: int, message: str) -> None:
        if request_id != self._startup_plugin_load_request_id:
            return
        self._startup_plugin_load_state = "failed"
        self._startup_plugin_load_error = message or "插件加载失败"
        self._sync_startup_plugin_loading_ui()
        self._startup_plugin_pending_tab_restore_key = ""
        if self._startup_plugin_pending_player_restore:
            self._startup_plugin_pending_player_restore = False
            self.config.last_active_window = "main"
            self._save_config()
        self._refresh_navigation_tabs()

    def _retry_startup_plugin_load(self) -> None:
        if self._startup_plugin_load_state == "loading":
            return
        self._startup_plugin_load_state = "loading"
        self._startup_plugin_load_error = ""
        self._startup_plugin_load_started = False
        self._plugin_definitions = []
        self._rebuild_spider_plugin_tabs()
        self._sync_startup_plugin_loading_ui()
        self._refresh_navigation_tabs()
        self._start_startup_plugin_load()

    def _handle_telegram_item_open_requested(self, item) -> None:
        vod_id = item.vod_id
        self._start_open_request(lambda: self.telegram_controller.build_request(vod_id))

    def _handle_telegram_open_requested(self, vod_id: str) -> None:
        self._start_open_request(lambda: self.telegram_controller.build_request(vod_id))

    def _handle_bilibili_item_open_requested(self, item) -> None:
        if getattr(item, "vod_tag", "") == "folder":
            if self.bilibili_page is not None:
                self._open_media_folder(self.bilibili_page, self.bilibili_controller, item)
            return
        vod_id = item.vod_id
        self._start_open_request(lambda: self.bilibili_controller.build_request(vod_id))

    def _handle_live_item_open_requested(self, item) -> None:
        if getattr(item, "vod_tag", "") == "folder":
            self._open_media_folder(self.live_page, self.live_controller, item)
            return
        vod_id = item.vod_id
        self._start_open_request(lambda: self.live_controller.build_request(vod_id))

    def _handle_emby_item_open_requested(self, item) -> None:
        if getattr(item, "vod_tag", "") == "folder":
            if self.emby_page is not None:
                self._open_media_folder(self.emby_page, self.emby_controller, item)
            return
        vod_id = item.vod_id
        self._start_open_request(lambda: self.emby_controller.build_request(vod_id))

    def _handle_jellyfin_item_open_requested(self, item) -> None:
        if getattr(item, "vod_tag", "") == "folder":
            if self.jellyfin_page is not None:
                self._open_media_folder(self.jellyfin_page, self.jellyfin_controller, item)
            return
        vod_id = item.vod_id
        self._start_open_request(lambda: self.jellyfin_controller.build_request(vod_id))

    def _handle_feiniu_item_open_requested(self, item) -> None:
        if getattr(item, "vod_tag", "") == "folder":
            if self.feiniu_page is not None:
                self._open_media_folder(self.feiniu_page, self.feiniu_controller, item)
            return
        vod_id = item.vod_id
        self._start_open_request(lambda: self.feiniu_controller.build_request(vod_id))

    def _handle_pansou_item_open_requested(self, item) -> None:
        if self.pansou_controller is None:
            return
        self._pansou_resolve_request_id += 1
        request_id = self._pansou_resolve_request_id
        self.global_search_status_label.setText("打开中...")

        def run() -> None:
            try:
                path = self.pansou_controller.resolve_search_result(item)
            except Exception as exc:
                if self._is_window_alive():
                    self._pansou_resolve_request_signals.failed.emit(request_id, str(exc))
                return
            if self._is_window_alive():
                self._pansou_resolve_request_signals.succeeded.emit(request_id, path)

        threading.Thread(target=run, daemon=True).start()

    def _searchable_tab_definitions(self) -> list[_TabDefinition]:
        return [definition for definition in self._all_tab_definitions() if definition.search_controller is not None]

    def _build_drive_detail_request(self, link: str) -> OpenPlayerRequest:
        if self._drive_detail_loader is None:
            raise ValueError("当前未配置网盘解析")
        try:
            payload = self._drive_detail_loader(link)
            detail = _map_vod_item(payload["list"][0])
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"没有可播放的项目: {link}") from exc
        playlist = build_detail_playlist(detail)
        if not playlist:
            raise ValueError(f"没有可播放的项目: {detail.vod_name or link}")
        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            clicked_index=0,
            source_kind="telegram",
            source_mode="detail",
            source_vod_id=link,
            detail_resolver=getattr(self.browse_controller, "resolve_folder_play_item", None),
        )

    def _build_offline_download_request(self, link: str) -> OpenPlayerRequest:
        if self._offline_download_detail_loader is None:
            raise ValueError("当前未配置磁力链接解析")
        try:
            payload = self._offline_download_detail_loader(link)
            detail = _map_vod_item(payload["list"][0])
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"没有可播放的项目: {link}") from exc
        playlist = build_detail_playlist(detail)
        if not playlist:
            raise ValueError(f"没有可播放的项目: {detail.vod_name or link}")
        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            clicked_index=0,
            source_kind="browse",
            source_mode="detail",
            source_vod_id=link,
            detail_resolver=getattr(self.browse_controller, "resolve_folder_play_item", None),
        )

    def _build_direct_parse_request(self, url: str) -> OpenPlayerRequest:
        if self._playback_parser_service is None:
            raise ValueError("当前未配置内置解析")
        history_loader, history_saver = self._direct_parse_history_hooks(url)
        danmaku_controller = self._build_direct_parse_danmaku_controller()

        def resolve_with_ytdlp(current_item: PlayItem, source_url: str):
            yt_dlp = self._yt_dlp_service
            if yt_dlp is None or not yt_dlp.is_available():
                raise ValueError("yt-dlp 不可用")
            selected_quality_id = current_item.selected_playback_quality_id or ""
            if selected_quality_id.startswith("ytdlp_"):
                return yt_dlp.resolve_for_quality(source_url, selected_quality_id)
            return yt_dlp.resolve(source_url, max_height=None)

        def load_item(
            session_or_item,
            item: PlayItem | None = None,
        ):
            session = session_or_item if item is not None else None
            current_item = item or session_or_item
            source_url = (current_item.original_url or current_item.vod_id or url).strip() or url
            try:
                result = self._playback_parser_service.resolve(
                    "",
                    source_url,
                    preferred_key=getattr(self.config, "preferred_parse_key", ""),
                )
                current_item.url = result.url
                current_item.original_url = source_url
                current_item.headers = dict(result.headers)
            except ValueError:
                yt_result = resolve_with_ytdlp(current_item, source_url)
                self._yt_dlp_service.apply_result(
                    yt_result,
                    vod=None if session is None else session.vod,
                    item=current_item,
                    source_url=source_url,
                )
            current_item.parse_required = True
            if danmaku_controller is not None:
                danmaku_controller.maybe_resolve(current_item)
            return None

        detailed_request = self._build_direct_parse_detail_request(url, load_item, danmaku_controller)
        if detailed_request is not None:
            return detailed_request

        item = PlayItem(
            title="解析播放",
            url="",
            original_url=url,
            vod_id=url,
            parse_required=True,
        )

        return OpenPlayerRequest(
            vod=VodItem(vod_id=url, vod_name=url),
            playlist=[item],
            clicked_index=0,
            source_kind="direct_parse",
            source_mode="parse",
            source_vod_id=url,
            playback_loader=load_item,
            async_playback_loader=True,
            use_local_history=False,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
            danmaku_controller=danmaku_controller,
        )

    def _build_ytdlp_parse_request(self, url: str) -> OpenPlayerRequest:
        if self._yt_dlp_service is None or not self._yt_dlp_service.is_available():
            raise ValueError("yt-dlp 不可用")
        history_loader, history_saver = self._direct_parse_history_hooks(url)

        def load_item(session, current_item: PlayItem):
            source_url = (current_item.original_url or current_item.vod_id or url).strip() or url
            selected_quality_id = current_item.selected_playback_quality_id or ""
            if selected_quality_id.startswith("ytdlp_"):
                result = self._yt_dlp_service.resolve_for_quality(source_url, selected_quality_id)
            else:
                result = self._yt_dlp_service.resolve(source_url, max_height=None)
            self._yt_dlp_service.apply_result(
                result,
                vod=session.vod,
                item=current_item,
                source_url=source_url,
            )
            return None

        item = PlayItem(
            title=url,
            url="",
            original_url=url,
            vod_id=url,
            media_title=url,
            selected_playback_quality_id="",
            ytdl_format="",
            playback_qualities=[],
        )
        return OpenPlayerRequest(
            vod=VodItem(vod_id=url, vod_name=url),
            playlist=[item],
            clicked_index=0,
            source_kind="direct_parse",
            source_mode="ytdlp",
            source_vod_id=url,
            use_local_history=False,
            playback_loader=load_item,
            async_playback_loader=True,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )

    def _build_parse_request(self, url: str) -> OpenPlayerRequest:
        yt_dlp = self._yt_dlp_service
        if yt_dlp is not None and yt_dlp.is_available() and yt_dlp.can_resolve(url):
            return self._build_ytdlp_parse_request(url)
        return self._build_direct_parse_request(url)

    def _build_direct_parse_detail_request(self, url: str, load_item, danmaku_controller) -> OpenPlayerRequest | None:
        history_loader, history_saver = self._direct_parse_history_hooks(url)
        if self._direct_parse_detail_loader is None:
            return None
        try:
            payload = self._direct_parse_detail_loader(url)
        except Exception:
            return None
        playlist = self._build_direct_parse_playlist(payload)
        if not playlist:
            return None
        clicked_index = next((index for index, item in enumerate(playlist) if item.original_url == url), 0)
        return OpenPlayerRequest(
            vod=VodItem(
                vod_id=str(payload.get("vod_form") or url),
                vod_name=str(payload.get("vod_title") or url),
                vod_pic=str(payload.get("vod_pic") or ""),
                vod_remarks=str(payload.get("vod_updateTo") or ""),
                type_name=str(payload.get("vod_type") or ""),
                vod_content=str(payload.get("vod_desc") or ""),
                vod_year=str(payload.get("vod_year") or ""),
            ),
            playlist=playlist,
            clicked_index=clicked_index,
            source_kind="direct_parse",
            source_mode="parse",
            source_vod_id=url,
            source_clicked_vod_id=str(payload.get("vod_form") or url),
            playback_loader=load_item,
            async_playback_loader=True,
            use_local_history=False,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
            danmaku_controller=danmaku_controller,
        )

    def _direct_parse_history_hooks(self, url: str) -> tuple[Any, Any]:
        history_loader = None
        if self._direct_parse_playback_history_loader is not None:
            def history_loader(source_vod_id=url):
                return self._direct_parse_playback_history_loader(source_vod_id)
        history_saver = None
        if self._direct_parse_playback_history_saver is not None:
            def history_saver(payload, source_vod_id=url):
                return self._direct_parse_playback_history_saver(source_vod_id, payload)
        return history_loader, history_saver

    def _build_direct_parse_danmaku_controller(self) -> object | None:
        if self._direct_parse_danmaku_loader is None:
            return None
        return DirectParseDanmakuController(load=self._direct_parse_danmaku_loader)

    def _build_direct_parse_playlist(self, payload: dict[str, Any]) -> list[PlayItem]:
        playlist: list[PlayItem] = []
        for episode in payload.get("vod_episodes") or []:
            if not isinstance(episode, dict):
                continue
            episode_url = str(episode.get("url") or "").strip()
            if not episode_url:
                continue
            playlist.append(
                PlayItem(
                    title=str(episode.get("name") or episode.get("title") or f"第{len(playlist) + 1}集"),
                    url="",
                    original_url=episode_url,
                    vod_id=episode_url,
                    parse_required=True,
                    index=len(playlist),
                )
            )
        return playlist

    def _start_direct_open_from_global_search(self, keyword: str) -> bool:
        if _looks_like_offline_download_link(keyword):
            self._start_open_request(lambda: self._build_offline_download_request(keyword))
            return True
        if not _looks_like_http_url(keyword):
            return False
        if _looks_like_drive_share_link(keyword):
            self._start_open_request(lambda: self._build_drive_detail_request(keyword))
            return True
        yt_dlp = self._yt_dlp_service
        if yt_dlp is not None and yt_dlp.is_available() and yt_dlp.can_resolve(keyword):
            self._start_open_request(lambda: self._build_parse_request(keyword))
            return True
        if self._playback_parser_service is None:
            self.show_error("当前未配置内置解析")
            return True
        self._start_open_request(lambda: self._build_parse_request(keyword))
        return True

    def _start_global_search(self) -> None:
        keyword = self.global_search_edit.text().strip()
        if not keyword:
            return
        self._hide_global_search_popup()
        if self._start_direct_open_from_global_search(keyword):
            return
        self._record_global_search_history(keyword)
        searchable = self._searchable_tab_definitions()
        if not searchable:
            self.global_search_status_label.setText("无可搜索来源")
            return
        self._global_search_request_id += 1
        request_id = self._global_search_request_id
        self._global_search_active = True
        self._global_search_in_progress = True
        self._global_search_keyword = keyword
        self._global_search_results = {}
        self._global_search_pending_keys = {definition.key for definition in searchable}
        self.global_search_status_label.setText("搜索中...")
        self._refresh_visible_tabs()
        self._sync_global_search_action_state()
        for definition in searchable:
            threading.Thread(
                target=self._run_global_search,
                args=(request_id, definition, keyword),
                daemon=True,
            ).start()

    def _run_global_search(self, request_id: int, definition: _TabDefinition, keyword: str) -> None:
        self._run_global_search_page(request_id, definition, keyword, 1)

    def _run_global_search_page(
        self,
        request_id: int,
        definition: _TabDefinition,
        keyword: str,
        page_number: int,
    ) -> None:
        controller = definition.search_controller
        if controller is None:
            if self._is_window_alive():
                self._global_search_signals.failed.emit(request_id, definition.key)
            return
        try:
            items, total = controller.search_items(keyword, page_number)
        except Exception:
            if self._is_window_alive():
                self._global_search_signals.failed.emit(request_id, definition.key)
            return
        if self._is_window_alive():
            self._global_search_signals.succeeded.emit(
                request_id,
                _GlobalSearchResult(
                    key=definition.key,
                    title=definition.title,
                    page=cast(PosterGridPage, definition.page),
                    items=list(items),
                    total=total,
                    page_number=page_number,
                ),
            )

    def _handle_global_search_succeeded(self, request_id: int, result: _GlobalSearchResult) -> None:
        if request_id != self._global_search_request_id:
            return
        initial_request = result.key in self._global_search_pending_keys
        if initial_request and not result.items:
            self._global_search_results.pop(result.key, None)
            self._refresh_visible_tabs()
        else:
            self._show_global_search_result(result)
            if initial_request:
                self._refresh_visible_tabs()
        if initial_request:
            self._finish_global_search_result(result.key)

    def _handle_global_search_failed(self, request_id: int, key: str) -> None:
        if request_id != self._global_search_request_id:
            return
        initial_request = key in self._global_search_pending_keys
        if initial_request:
            self._global_search_results.pop(key, None)
            self._refresh_visible_tabs()
            self._finish_global_search_result(key)

    def _finish_global_search_result(self, key: str) -> None:
        self._global_search_pending_keys.discard(key)
        if self._global_search_pending_keys:
            return
        self._global_search_in_progress = False
        if self.nav_tabs.count() > 0:
            self.global_search_status_label.setText("")
        else:
            self.global_search_status_label.setText("无搜索结果")
        self._sync_global_search_action_state()

    def _clear_global_search(self) -> None:
        if not self._global_search_active and not self.global_search_edit.text().strip():
            return
        if self._global_search_active:
            self._global_search_request_id += 1
        self._global_search_active = False
        self._global_search_in_progress = False
        self._global_search_keyword = ""
        self._global_search_results = {}
        self._global_search_pending_keys = set()
        self.global_search_edit.blockSignals(True)
        self.global_search_edit.clear()
        self.global_search_edit.blockSignals(False)
        self.global_search_status_label.setText("")
        for definition in self._all_tab_definitions():
            if isinstance(definition.page, PosterGridPage):
                definition.page.clear_external_results()
        self._refresh_visible_tabs()
        self._sync_global_search_action_state()
        self._hide_global_search_popup()

    def _show_global_search_result(self, result: _GlobalSearchResult) -> None:
        result.page.show_external_results(
            result.items,
            result.total,
            page=result.page_number,
            empty_message="无搜索结果",
            page_loader=self._build_global_search_page_loader(result.key),
        )
        self._global_search_results[result.key] = result

    def _build_global_search_page_loader(self, key: str):
        def load_page(page_number: int) -> None:
            self._load_global_search_page(key, page_number)

        return load_page

    def _load_global_search_page(self, key: str, page_number: int) -> None:
        if not self._global_search_active or not self._global_search_keyword:
            return
        definition = next((item for item in self._all_tab_definitions() if item.key == key), None)
        if definition is None:
            return
        request_id = self._global_search_request_id
        keyword = self._global_search_keyword
        threading.Thread(
            target=self._run_global_search_page,
            args=(request_id, definition, keyword, page_number),
            daemon=True,
        ).start()

    def _create_plugin_page_entry(
        self,
        definition,
    ) -> tuple[PosterGridPage, _PluginController, str, _TabDefinition]:
        controller = cast(_PluginController, _plugin_value(definition, "controller"))
        plugin_id = str(_plugin_value(definition, "id") or "")
        title = str(_plugin_value(definition, "title") or "插件")
        page = PosterGridPage(
            controller,
            click_action="open",
            search_enabled=bool(_plugin_value(definition, "search_enabled")),
            initial_category_id=self._initial_category_id_for_tab(f"plugin:{plugin_id}"),
        )
        page.item_open_requested.connect(
            lambda item, controller=controller, plugin_id=plugin_id: self._open_spider_item(
                controller,
                plugin_id,
                item,
            )
        )
        page.unauthorized.connect(self.logout_requested.emit)
        page.selected_category_changed.connect(
            lambda category_id, page=page: self._handle_selected_category_changed(page, category_id)
        )
        tab_definition = _TabDefinition(f"plugin:{plugin_id}", title, page, controller)
        return page, controller, plugin_id, tab_definition

    def _append_plugin_definition(self, definition) -> None:
        plugin_id = str(_plugin_value(definition, "id") or "")
        if any(current_plugin_id == plugin_id for _page, _controller, current_plugin_id in self._plugin_pages):
            return
        self._plugin_definitions.append(definition)
        self._append_plugin_page_from_definition(definition)

    def _append_plugin_page_from_definition(self, definition) -> None:
        page, controller, plugin_id, tab_definition = self._create_plugin_page_entry(definition)
        self._plugin_pages.append((page, controller, plugin_id))
        self._plugin_tab_definitions.append(tab_definition)

    def _load_plugin_definitions_with_manager(self, method_name: str, *args):
        if self._plugin_manager is None:
            return []
        method = getattr(self._plugin_manager, method_name, None)
        if not callable(method):
            return []
        try:
            loaded_plugins = method(
                *args,
                drive_detail_loader=self._drive_detail_loader,
                offline_download_detail_loader=self._offline_download_detail_loader,
            )
        except TypeError as exc:
            if "offline_download_detail_loader" not in str(exc):
                if "drive_detail_loader" not in str(exc):
                    raise
                loaded_plugins = method(
                    *args,
                    drive_detail_loader=self._drive_detail_loader,
                )
            else:
                try:
                    loaded_plugins = method(
                        *args,
                        drive_detail_loader=self._drive_detail_loader,
                    )
                except TypeError as drive_exc:
                    if "drive_detail_loader" not in str(drive_exc):
                        raise
                    loaded_plugins = method(*args)
        if isinstance(loaded_plugins, Iterable):
            return list(loaded_plugins)
        return []

    def _reload_changed_plugin_tabs(self, changed_plugin_ids: list[str]) -> bool:
        if self._plugin_manager is None or not changed_plugin_ids:
            return False
        list_plugins = getattr(self._plugin_manager, "list_plugins", None)
        load_plugins = getattr(self._plugin_manager, "load_plugins", None)
        if not callable(list_plugins) or not callable(load_plugins):
            return False

        current_plugins = list_plugins()
        enabled_order = [str(plugin.id) for plugin in current_plugins if getattr(plugin, "enabled", False)]
        changed_id_set = {str(plugin_id) for plugin_id in changed_plugin_ids if str(plugin_id)}
        changed_enabled_ids = [plugin_id for plugin_id in enabled_order if plugin_id in changed_id_set]
        loaded_definitions = self._load_plugin_definitions_with_manager("load_plugins", changed_enabled_ids)
        loaded_by_id = {str(_plugin_value(definition, "id") or ""): definition for definition in loaded_definitions}

        existing_definitions = {
            str(_plugin_value(definition, "id") or ""): definition
            for definition in self._plugin_definitions
        }
        existing_pages = {
            current_plugin_id: (page, controller, current_plugin_id)
            for page, controller, current_plugin_id in self._plugin_pages
        }
        existing_tab_definitions = {definition.key.removeprefix("plugin:"): definition for definition in self._plugin_tab_definitions}

        for plugin_id in changed_id_set:
            if plugin_id in loaded_by_id:
                page_entry = existing_pages.pop(plugin_id, None)
                if page_entry is not None:
                    page_entry[0].deleteLater()
                existing_tab_definitions.pop(plugin_id, None)
                existing_definitions[plugin_id] = loaded_by_id[plugin_id]
                page, controller, current_plugin_id, tab_definition = self._create_plugin_page_entry(loaded_by_id[plugin_id])
                existing_pages[plugin_id] = (page, controller, current_plugin_id)
                existing_tab_definitions[plugin_id] = tab_definition
                continue
            if plugin_id not in enabled_order:
                page_entry = existing_pages.pop(plugin_id, None)
                if page_entry is not None:
                    page_entry[0].deleteLater()
                existing_tab_definitions.pop(plugin_id, None)
                existing_definitions.pop(plugin_id, None)

        ordered_definitions = [existing_definitions[plugin_id] for plugin_id in enabled_order if plugin_id in existing_definitions]
        ordered_pages = [existing_pages[plugin_id] for plugin_id in enabled_order if plugin_id in existing_pages]
        ordered_tabs = [existing_tab_definitions[plugin_id] for plugin_id in enabled_order if plugin_id in existing_tab_definitions]

        self._plugin_definitions = ordered_definitions
        self._plugin_pages = ordered_pages
        self._plugin_tab_definitions = ordered_tabs
        self._refresh_visible_tabs()
        return True

    def _rebuild_spider_plugin_tabs(self) -> None:
        for page, _controller, _plugin_id in self._plugin_pages:
            page.deleteLater()
        self._plugin_pages = []
        self._plugin_tab_definitions = []
        for definition in self._plugin_definitions:
            self._append_plugin_page_from_definition(definition)
        self._refresh_visible_tabs()

    def _build_placeholder_player_request(
        self, item: Any, *, source_kind: str = "plugin", source_key: str = "",
    ) -> OpenPlayerRequest:
        placeholder_vod = VodItem(
            vod_id=getattr(item, "vod_id", ""),
            vod_name=getattr(item, "vod_name", ""),
            vod_pic=getattr(item, "vod_pic", ""),
            vod_remarks=getattr(item, "vod_remarks", ""),
            type_name=getattr(item, "type_name", ""),
            category_name=getattr(item, "category_name", ""),
            vod_content=getattr(item, "vod_content", ""),
            vod_year=getattr(item, "vod_year", ""),
            vod_area=getattr(item, "vod_area", ""),
            vod_lang=getattr(item, "vod_lang", ""),
            vod_director=getattr(item, "vod_director", ""),
            vod_actor=getattr(item, "vod_actor", ""),
        )
        return OpenPlayerRequest(
            vod=placeholder_vod,
            playlist=[],
            clicked_index=0,
            source_kind=source_kind,
            source_key=source_key,
            source_mode="detail",
            source_vod_id=placeholder_vod.vod_id,
            use_local_history=False,
            initial_log_message="正在加载详情...",
            is_placeholder=True,
        )

    @staticmethod
    def _apply_request_fallback_metadata(request: OpenPlayerRequest, item: Any) -> OpenPlayerRequest:
        fallback_type_name = str(getattr(item, "type_name", "") or "").strip()
        fallback_category_name = str(getattr(item, "category_name", "") or "").strip()
        fallback_vod_name = str(getattr(item, "vod_name", "") or "").strip()
        if fallback_type_name and not request.vod.type_name:
            request.vod.type_name = fallback_type_name
        if fallback_category_name and not request.vod.category_name:
            request.vod.category_name = fallback_category_name
        if fallback_vod_name and not request.vod.vod_name:
            request.vod.vod_name = fallback_vod_name
        resolved_media_title = (request.vod.vod_name or fallback_vod_name).strip()
        playlists = list(request.playlists or [])
        if not playlists and request.playlist:
            playlists = [request.playlist]
        for playlist in playlists:
            for play_item in playlist:
                if fallback_type_name and not play_item.type_name:
                    play_item.type_name = fallback_type_name
                if fallback_category_name and not play_item.category_name:
                    play_item.category_name = fallback_category_name
                if resolved_media_title and not play_item.media_title:
                    play_item.media_title = resolved_media_title
        return request

    def _current_player_session_is_placeholder(self) -> bool:
        if self.player_window is None:
            return False
        session = getattr(self.player_window, "session", None)
        if session is None:
            return False
        if isinstance(session, dict):
            return bool(session.get("is_placeholder"))
        return bool(getattr(session, "is_placeholder", False))

    def _build_restore_placeholder_request(self) -> OpenPlayerRequest | None:
        source_kind = self.config.last_playback_source or "browse"
        if source_kind != "plugin":
            return None
        plugin_key = self.config.last_playback_source_key or ""
        plugin_title = next(
            (
                definition.title
                for definition in self._plugin_tab_definitions
                if definition.key == f"plugin:{plugin_key}"
            ),
            "",
        )
        vod_id = self.config.last_playback_vod_id or plugin_key
        vod_name = plugin_title or self.config.last_playback_vod_id or "恢复播放"
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name=vod_name),
            playlist=[],
            clicked_index=0,
            source_kind=source_kind,
            source_key=plugin_key,
            source_mode=self.config.last_playback_mode or "detail",
            source_path=self.config.last_playback_path,
            source_vod_id=self.config.last_playback_vod_id or vod_id,
            source_clicked_vod_id=self.config.last_playback_clicked_vod_id,
            use_local_history=False,
            initial_log_message="正在恢复播放...",
            is_placeholder=True,
        )

    def _open_restore_placeholder_if_needed(self) -> None:
        if self.player_window is not None:
            return
        placeholder_request = self._build_restore_placeholder_request()
        if placeholder_request is None:
            return
        self._open_player_immediately(placeholder_request, restore_paused_state=True)

    def _discard_restore_placeholder_and_return_to_main(self) -> None:
        if not self._current_player_session_is_placeholder():
            return
        close_player = getattr(self.player_window, "close", None) if self.player_window is not None else None
        if callable(close_player):
            close_player()
        self.player_window = None
        if self.isHidden():
            self._show_main_again()

    def _open_spider_item(self, controller, plugin_id: str, item: Any) -> None:
        placeholder_request = self._build_placeholder_player_request(item, source_kind="plugin", source_key=plugin_id)
        self._open_player_immediately(placeholder_request)

        def build_request() -> OpenPlayerRequest:
            request = controller.build_request(getattr(item, "vod_id", ""))
            request.source_kind = "plugin"
            request.source_key = plugin_id
            return self._apply_request_fallback_metadata(request, item)

        self._start_plugin_open_request(build_request)

    def _open_plugin_manager(self) -> None:
        if self._plugin_manager is None:
            return
        self._dismiss_visible_global_search_popup()
        self._close_plugin_overflow_drawer()
        self._close_help_dialog()
        dialog = PluginManagerDialog(self._plugin_manager, self)
        dialog.exec()
        if not bool(getattr(dialog, "plugin_tabs_dirty", False)):
            return
        changed_plugin_ids = [str(plugin_id) for plugin_id in getattr(dialog, "changed_plugin_ids", []) if str(plugin_id)]
        if self._reload_changed_plugin_tabs(changed_plugin_ids):
            return
        self._plugin_definitions = self._load_plugin_definitions_with_manager("load_enabled_plugins")
        self._rebuild_spider_plugin_tabs()

    def _open_live_source_manager(self) -> None:
        if self._live_source_manager is None:
            return
        self._dismiss_visible_global_search_popup()
        self._close_plugin_overflow_drawer()
        self._close_help_dialog()
        dialog = LiveSourceManagerDialog(self._live_source_manager, self)
        dialog.exec()
        self.live_page.reload_categories()

    def _open_advanced_settings(self) -> None:
        self._dismiss_visible_global_search_popup()
        self._close_plugin_overflow_drawer()
        self._close_help_dialog()
        dialog = AdvancedSettingsDialog(self.config, self._save_config, self)
        dialog.exec()

    def _open_media_folder(self, page: PosterGridPage, controller: Any, item: Any) -> None:
        self._start_media_load(
            page,
            lambda: controller.load_folder_items(item.vod_id),
            empty_message="当前文件夹暂无内容",
            push_breadcrumb=(item.vod_id, item.vod_name),
        )

    def _handle_media_breadcrumb_requested(
        self,
        page: PosterGridPage,
        controller: Any,
        node_id: str,
        kind: str,
        index: int,
    ) -> None:
        if kind == "folder":
            self._start_media_load(
                page,
                lambda: controller.load_folder_items(node_id),
                empty_message="当前文件夹暂无内容",
                trim_breadcrumbs_to=index,
            )
            return
        category_id = page.selected_category_id
        if not category_id:
            return
        self._start_media_load(
            page,
            lambda: controller.load_items(category_id, 1),
            empty_message="当前分类暂无内容",
            trim_breadcrumbs_to=1,
        )

    def _find_plugin_controller(self, plugin_id: int):
        for definition in self._plugin_definitions:
            if _plugin_value(definition, "id") == plugin_id:
                return _plugin_value(definition, "controller")
        return None

    def open_history_detail(self, record: HistoryRecord) -> None:
        if record.source_kind == "direct_parse":
            self._start_open_request(lambda: self._build_parse_request(record.key))
            return
        if record.source_kind == "spider_plugin":
            plugin_id = record.source_key or str(record.source_plugin_id or "")
            controller = self._plugin_controller_by_id(plugin_id)
            if controller is None:
                self.show_error(f"没有可播放的项目: {record.source_name or record.source_plugin_name or record.key}")
                return
            def build_request():
                request = controller.build_request(record.key)
                request.source_kind = "plugin"
                request.source_key = plugin_id
                if request.playback_history_loader is None:
                    request.playlist_index = record.playlist_index
                    request.clicked_index = record.episode
                return request

            self._start_open_request(build_request)
            return
        if record.source_kind == "emby":
            self._start_open_request(lambda: self.emby_controller.build_request(record.key))
            return
        if record.source_kind == "bilibili":
            self._start_open_request(lambda: self.bilibili_controller.build_request(record.key))
            return
        if record.source_kind == "jellyfin":
            self._start_open_request(lambda: self.jellyfin_controller.build_request(record.key))
            return
        if record.source_kind == "feiniu":
            self._start_open_request(lambda: self.feiniu_controller.build_request(record.key))
            return
        self._start_open_request(lambda: self.browse_controller.build_request_from_detail(record.key))

    def _start_open_request(self, builder) -> int:
        self._open_request_id += 1
        request_id = self._open_request_id

        def run() -> None:
            try:
                request = builder()
            except Exception as exc:
                if self._is_window_alive():
                    self._open_request_signals.failed.emit(request_id, str(exc))
                return
            if self._is_window_alive():
                self._open_request_signals.succeeded.emit(request_id, request)

        threading.Thread(target=run, daemon=True).start()
        return request_id

    def _start_plugin_open_request(self, builder) -> int:
        self._plugin_open_request_id += 1
        request_id = self._plugin_open_request_id

        def run() -> None:
            try:
                request = builder()
            except Exception as exc:
                if self._is_window_alive():
                    self._plugin_open_request_signals.failed.emit(request_id, str(exc))
                return
            if self._is_window_alive():
                self._plugin_open_request_signals.succeeded.emit(request_id, request)

        threading.Thread(target=run, daemon=True).start()
        return request_id

    def _handle_open_request_succeeded(self, request_id: int, request: OpenPlayerRequest) -> None:
        if request_id != self._open_request_id:
            return
        self.open_player(request)

    def _handle_open_request_failed(self, request_id: int, message: str) -> None:
        if request_id != self._open_request_id:
            return
        if self.player_window is not None and getattr(self.player_window, "session", None) is not None:
            self._append_player_status_log(f"详情加载失败: {message}")
            return
        self.show_error(message)

    def _handle_plugin_open_request_succeeded(self, request_id: int, request: OpenPlayerRequest) -> None:
        if request_id != self._plugin_open_request_id:
            return
        self.open_player(request)

    def _handle_plugin_open_request_failed(self, request_id: int, message: str) -> None:
        if request_id != self._plugin_open_request_id:
            return
        self._append_player_status_log(f"详情加载失败: {message}")

    def _handle_pansou_resolve_succeeded(self, request_id: int, path: str) -> None:
        if request_id != self._pansou_resolve_request_id:
            return
        self._clear_global_search()
        self.show_browse_path(path)

    def _handle_pansou_resolve_failed(self, request_id: int, message: str) -> None:
        if request_id != self._pansou_resolve_request_id:
            return
        self.global_search_status_label.setText("")
        self.show_error(message)

    def _start_media_load(
        self,
        page: PosterGridPage,
        loader,
        *,
        empty_message: str,
        push_breadcrumb: tuple[str, str] | None = None,
        trim_breadcrumbs_to: int | None = None,
    ) -> int:
        self._media_request_id += 1
        request_id = self._media_request_id

        def run() -> None:
            try:
                items, total = loader()
            except Exception as exc:
                if self._is_window_alive():
                    self._media_request_signals.failed.emit(request_id, str(exc))
                return
            if self._is_window_alive():
                self._media_request_signals.succeeded.emit(
                    request_id,
                    _MediaLoadResult(
                        page=page,
                        items=list(items),
                        total=total,
                        empty_message=empty_message,
                        push_breadcrumb=push_breadcrumb,
                        trim_breadcrumbs_to=trim_breadcrumbs_to,
                    ),
                )

        threading.Thread(target=run, daemon=True).start()
        return request_id

    def _handle_media_load_succeeded(self, request_id: int, result: _MediaLoadResult) -> None:
        if request_id != self._media_request_id:
            return
        result.page.show_items(result.items, result.total, page=1, empty_message=result.empty_message)
        if result.push_breadcrumb is not None:
            breadcrumb_id, label = result.push_breadcrumb
            result.page.push_folder_breadcrumb(breadcrumb_id, label)
        if result.trim_breadcrumbs_to is not None:
            result.page.trim_folder_breadcrumbs(result.trim_breadcrumbs_to)

    def _handle_media_load_failed(self, request_id: int, message: str) -> None:
        if request_id != self._media_request_id:
            return
        self.show_error(message)

    def _is_window_alive(self) -> bool:
        return self._can_deliver_async_result()

    def _next_player_session_request_id(self) -> int:
        self._player_session_request_id += 1
        return self._player_session_request_id

    def _create_player_session(self, request):
        return self.player_controller.create_session(
            request.vod,
            request.playlist,
            request.clicked_index,
            playlists=request.playlists,
            playlist_index=request.playlist_index,
            source_groups=request.source_groups,
            source_group_index=request.source_group_index,
            source_index=request.source_index,
            detail_resolver=request.detail_resolver,
            resolved_vod_by_id=request.resolved_vod_by_id,
            use_local_history=request.use_local_history,
            restore_history=request.restore_history,
            playback_loader=request.playback_loader,
            async_playback_loader=request.async_playback_loader,
            detail_action_runner=request.detail_action_runner,
            detail_field_runner=request.detail_field_runner,
            metadata_hydrator=request.metadata_hydrator,
            metadata_scrape_service=request.metadata_scrape_service,
            metadata_binding_repository=request.metadata_binding_repository,
            episode_title_enhancer=request.episode_title_enhancer,
            danmaku_controller=request.danmaku_controller,
            playback_progress_reporter=request.playback_progress_reporter,
            playback_stopper=request.playback_stopper,
            playback_history_loader=request.playback_history_loader,
            playback_history_saver=request.playback_history_saver,
            initial_log_message=request.initial_log_message,
            is_placeholder=request.is_placeholder,
        )

    def _append_player_status_log(self, message: str) -> None:
        if not message:
            return
        if self.player_window is None:
            return
        append_status_log = getattr(self.player_window, "append_status_log", None)
        if callable(append_status_log):
            append_status_log(message)

    def _plugin_page_context_by_id(self, plugin_id: str) -> tuple[PosterGridPage, _PluginController] | None:
        for page, controller, current_plugin_id in self._plugin_pages:
            if current_plugin_id == plugin_id:
                return page, controller
        return None

    def _prepare_request_for_open(self, request: OpenPlayerRequest) -> OpenPlayerRequest:
        if (
            request.metadata_hydrator is None
            and self._metadata_hydrator_factory is not None
            and request.source_kind in {"browse", "emby", "jellyfin", "feiniu", "bilibili"}
        ):
            request.metadata_hydrator = self._metadata_hydrator_factory(
                request=request,
                source_kind=request.source_kind,
                source_key=request.source_key,
                vod=request.vod,
            )
        if (
            request.metadata_scrape_service is None
            and self._metadata_scrape_service_factory is not None
            and request.source_kind in {"browse", "plugin", "emby", "jellyfin", "feiniu", "bilibili"}
        ):
            request.metadata_scrape_service = self._metadata_scrape_service_factory(
                request=request,
                source_kind=request.source_kind,
                source_key=request.source_key,
                vod=request.vod,
            )
        if (
            request.episode_title_enhancer is None
            and self._episode_title_enhancer_factory is not None
            and request.source_kind == "browse"
        ):
            request.episode_title_enhancer = self._episode_title_enhancer_factory(
                request=request,
                source_kind=request.source_kind,
                source_key=request.source_key,
                vod=request.vod,
            )
        if request.metadata_binding_repository is None:
            request.metadata_binding_repository = self._metadata_binding_repository
        if request.detail_field_runner is not None:
            return request
        if request.source_kind == "plugin" and request.source_key:
            context = self._plugin_page_context_by_id(request.source_key)
            if context is None:
                return request
            page, controller = context
            request.detail_field_runner = (
                lambda item, action, page=page, controller=controller, plugin_id=request.source_key: self._run_plugin_detail_field_action(
                    controller,
                    page,
                    plugin_id,
                    item,
                    action,
                )
            )
            return request
        if request.source_kind == "bilibili" and self.bilibili_page is not None:
            request.detail_field_runner = (
                lambda item, action, page=self.bilibili_page: self._run_bilibili_detail_field_action(page, item, action)
            )
        return request

    def _run_plugin_detail_field_action(
        self,
        controller: _PluginController,
        page: PosterGridPage,
        plugin_id: str,
        item: PlayItem,
        action: PlaybackDetailFieldAction,
    ) -> None:
        if action.type == "detail":
            def build_request() -> OpenPlayerRequest:
                request = controller.build_request(action.value)
                request.source_kind = "plugin"
                request.source_key = plugin_id
                return request

            self._start_plugin_open_request(build_request)
            return
        if action.type == "link":
            if not QDesktopServices.openUrl(QUrl(action.value)):
                self._append_player_status_log(f"详情跳转失败[link]: 无法打开链接 {action.value}")
            return
        if action.type == "category":
            self._show_main_again()
            page.selected_category_id = action.value
            self.nav_tabs.setCurrentWidget(page)
            self._start_media_load(page, lambda: controller.load_items(action.value, 1), empty_message="当前分类暂无内容")
            return
        if action.type == "search":
            self._show_main_again()
            self.nav_tabs.setCurrentWidget(page)
            self._start_media_load(page, lambda: controller.search_items(action.value, 1), empty_message="无搜索结果")

    def _run_bilibili_detail_field_action(
        self,
        page: PosterGridPage,
        item: PlayItem,
        action: PlaybackDetailFieldAction,
    ) -> None:
        if action.type == "link":
            if not QDesktopServices.openUrl(QUrl(action.value)):
                self._append_player_status_log(f"详情跳转失败[link]: 无法打开链接 {action.value}")
            return
        if action.target != "bilibili" or action.type != "category":
            return
        self._show_main_again()
        page.selected_category_id = action.value
        self.nav_tabs.setCurrentWidget(page)
        self._start_media_load(page, lambda: self.bilibili_controller.load_items(action.value, 1), empty_message="当前分类暂无内容")

    def _open_player_immediately(self, request, restore_paused_state: bool = False) -> None:
        request = self._prepare_request_for_open(request)
        try:
            session = self._create_player_session(request)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self._next_player_session_request_id()
        self._apply_open_player(request, session, restore_paused_state=restore_paused_state)

    def _apply_open_player(self, request, session, restore_paused_state: bool = False) -> None:
        if self.player_window is None:
            try:
                self.player_window = PlayerWindow(
                    self.player_controller,
                    self.config,
                    self._save_config,
                    m3u8_ad_filter=self._m3u8_ad_filter,
                    playback_parser_service=self._playback_parser_service,
                    default_video_cover_loader=self._default_video_cover_loader,
                )
            except TypeError as exc:
                if "m3u8_ad_filter" not in str(exc) and "playback_parser_service" not in str(exc):
                    raise
                self.player_window = PlayerWindow(
                    self.player_controller,
                    self.config,
                    self._save_config,
                )
            if hasattr(self.player_window, "closed_to_main"):
                self.player_window.closed_to_main.connect(self._show_main_again)
        self._close_help_dialog()
        self.config.last_active_window = "player"
        self.config.last_playback_source = request.source_kind
        self.config.last_playback_source_key = request.source_key
        self.config.last_playback_mode = request.source_mode
        self.config.last_playback_path = request.source_path
        self.config.last_playback_vod_id = request.source_vod_id
        self.config.last_playback_clicked_vod_id = request.source_clicked_vod_id
        start_paused = self.config.last_player_paused if restore_paused_state else False
        if not restore_paused_state:
            self.config.last_player_paused = False
        self._remember_main_window_state_for_player()
        self.config.main_window_geometry = qbytearray_to_bytes(self.saveGeometry())
        self._save_config()
        self.player_window.open_session(session, start_paused=start_paused)
        self.player_window.show()
        self.player_window.raise_()
        self.player_window.activateWindow()
        self.hide()

    def open_player(self, request, restore_paused_state: bool = False) -> None:
        request = self._prepare_request_for_open(request)
        request_id = self._next_player_session_request_id()

        def run() -> None:
            try:
                session = self._create_player_session(request)
            except Exception as exc:
                if self._is_window_alive():
                    self._session_open_signals.failed.emit(request_id, str(exc), restore_paused_state)
                return
            if not self._is_window_alive():
                return
            self._session_open_signals.succeeded.emit(request_id, request, session, restore_paused_state)

        threading.Thread(target=run, daemon=True).start()

    def _handle_session_open_succeeded(self, request_id: int, request, session, restore_paused_state: bool) -> None:
        if request_id != self._player_session_request_id:
            return
        self._apply_open_player(request, session, restore_paused_state=restore_paused_state)

    def _handle_session_open_failed(self, request_id: int, message: str, restore_paused_state: bool) -> None:
        if request_id != self._player_session_request_id:
            return
        if restore_paused_state:
            self._discard_restore_placeholder_and_return_to_main()
            self.config.last_active_window = "main"
            self._save_config()
        self.show_error(message)

    def _show_main_again(self) -> None:
        if self.player_window is not None and getattr(self.player_window, "session", None) is None:
            self.player_window = None
        self.config.last_active_window = "main"
        self._save_config()
        self._restore_main_window_after_player()
        self.raise_()
        self.activateWindow()

    def _remember_main_window_state_for_player(self) -> None:
        self._main_window_was_maximized_before_player = self.isMaximized()

    def _restore_main_window_after_player(self) -> None:
        self._restore_saved_geometry()
        self.show()
        if self._main_window_was_maximized_before_player:
            self.showMaximized()
        self._refresh_main_window_layout()
        QTimer.singleShot(0, self._refresh_main_window_layout)

    def _restore_saved_geometry(self) -> None:
        geometry = self.config.main_window_geometry
        if not geometry:
            return
        self.restoreGeometry(to_qbytearray(geometry))

    def _refresh_main_window_layout(self) -> None:
        central_widget = self.centralWidget()
        if central_widget is None:
            return
        central_widget.updateGeometry()
        layout = central_widget.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()

    def show_or_restore_player(self) -> PlayerWindow | None:
        if self.player_window is not None and getattr(self.player_window, "session", None) is not None:
            self._close_help_dialog()
            self.config.last_active_window = "player"
            self._save_config()
            if hasattr(self.player_window, "resume_from_main"):
                self.player_window.resume_from_main()
            self.player_window.show()
            self.player_window.raise_()
            self.player_window.activateWindow()
            self.hide()
            return self.player_window
        self._start_restore_last_player()
        return None

    def restore_last_player(self) -> PlayerWindow | None:
        try:
            request = self._build_restore_request()
        except Exception:
            return None
        if request is None:
            return None
        self._next_player_session_request_id()
        session = self._create_player_session(request)
        self._apply_open_player(request, session, restore_paused_state=True)
        return self.player_window

    def _build_restore_request(self) -> OpenPlayerRequest | None:
        mode = self.config.last_playback_mode
        source = self.config.last_playback_source or "browse"
        if mode in {"parse", "ytdlp"} and source in {"direct", "direct_parse"} and self.config.last_playback_vod_id:
            return self._build_parse_request(self.config.last_playback_vod_id)
        if mode in {"detail", "custom"} and self.config.last_playback_vod_id:
            return self._build_detail_restore_request(source, self.config.last_playback_vod_id)
        if mode == "folder" and self.config.last_playback_path and self.config.last_playback_clicked_vod_id:
            clicked, items = self._find_restorable_folder_item(
                self.config.last_playback_path,
                self.config.last_playback_clicked_vod_id,
            )
            if clicked is None:
                return None
            return self.browse_controller.build_request_from_folder_item(clicked, items)
        return None

    def _start_restore_last_player(self) -> int:
        if (
            self._startup_plugin_load_state == "loading"
            and self.config.last_playback_source == "plugin"
            and self._plugin_controller_by_id(self.config.last_playback_source_key) is None
        ):
            self._startup_plugin_pending_player_restore = True
            return self._restore_request_id
        if self.config.last_playback_source == "plugin":
            self._open_restore_placeholder_if_needed()
        self._restore_request_id += 1
        request_id = self._restore_request_id

        def run() -> None:
            try:
                request = self._build_restore_request()
            except Exception:
                if self._is_window_alive():
                    self._restore_signals.failed.emit(request_id)
                return
            if self._is_window_alive():
                self._restore_signals.succeeded.emit(request_id, request)

        threading.Thread(target=run, daemon=True).start()
        return request_id

    def _handle_restore_succeeded(self, request_id: int, request: OpenPlayerRequest | None) -> None:
        if request_id != self._restore_request_id:
            return None
        if request is None:
            self.config.last_active_window = "main"
            self._save_config()
            return
        self.open_player(request, restore_paused_state=True)

    def _handle_restore_failed(self, request_id: int) -> None:
        if request_id != self._restore_request_id:
            return
        self._discard_restore_placeholder_and_return_to_main()
        self.config.last_active_window = "main"
        self._save_config()

    def _build_detail_restore_request(self, source: str, vod_id: str):
        if source == "telegram":
            return self.telegram_controller.build_request(vod_id)
        if source == "live":
            return self.live_controller.build_request(vod_id)
        if source == "emby":
            return self.emby_controller.build_request(vod_id)
        if source == "bilibili":
            return self.bilibili_controller.build_request(vod_id)
        if source == "jellyfin":
            return self.jellyfin_controller.build_request(vod_id)
        if source == "plugin":
            controller = self._plugin_controller_by_id(self.config.last_playback_source_key)
            if controller is None:
                raise ValueError("找不到已保存的插件来源")
            request = controller.build_request(vod_id)
            request.source_kind = "plugin"
            request.source_key = self.config.last_playback_source_key
            return request
        return self.browse_controller.build_request_from_detail(vod_id)

    def _plugin_controller_by_id(self, plugin_id: str) -> _PluginController | None:
        for _page, controller, current_plugin_id in self._plugin_pages:
            if current_plugin_id == plugin_id:
                return controller
        return None

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

    def _build_main_window_help_payload(self) -> tuple[list[tuple[str, str]], str]:
        system_info_rows = [
            (entry.label, entry.value)
            for entry in collect_system_info_entries()
        ]
        shortcut_entries = shortcut_entries_for("main_window", self.quit_shortcut.key())
        lines = ["系统信息"]
        lines.extend(f"{label}: {value}" for label, value in system_info_rows)
        lines.append("")
        lines.append("快捷键")
        lines.extend(f"{entry.key}: {entry.description}" for entry in shortcut_entries)
        return system_info_rows, "\n".join(lines)

    def _show_shortcut_help(self) -> None:
        system_info_rows, diagnostics_text = self._build_main_window_help_payload()
        dialog = show_shortcut_help_dialog(
            self,
            context="main_window",
            existing_dialog=self.help_dialog,
            quit_sequence=self.quit_shortcut.key(),
            system_info_rows=system_info_rows,
            diagnostics_text=diagnostics_text,
        )
        if dialog is self.help_dialog:
            return
        self.help_dialog = dialog
        dialog.destroyed.connect(self._clear_help_dialog_reference)

    def _clear_help_dialog_reference(self, *_args) -> None:
        self.help_dialog = None

    def _close_help_dialog(self) -> None:
        dialog = self.help_dialog
        if dialog is None:
            return
        self.help_dialog = None
        dialog.close()

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

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._handle_global_search_global_mouse_press(event.globalPosition().toPoint())
        if not isinstance(watched, QObject):
            return False
        return super().eventFilter(cast(QObject, watched), event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        app = QApplication.instance()
        if app is not None and not self._app_event_filter_installed:
            app.installEventFilter(self)
            self._app_event_filter_installed = True
        if app is not None and not self._app_state_signal_connected:
            app.applicationStateChanged.connect(self._handle_application_state_changed)
            self._app_state_signal_connected = True
        self._refresh_navigation_tabs()
        self._start_startup_plugin_load()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_navigation_tabs()
        if self._plugin_overflow_drawer.isVisible():
            self._position_plugin_overflow_drawer()
        self._position_global_search_popup()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._position_global_search_popup()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._deactivate_async_guard()
        self._startup_plugin_load_request_id += 1
        self._close_plugin_overflow_drawer()
        self._hide_global_search_popup()
        app = QApplication.instance()
        if app is not None and self._app_event_filter_installed:
            app.removeEventFilter(self)
            self._app_event_filter_installed = False
        if app is not None and self._app_state_signal_connected:
            app.applicationStateChanged.disconnect(self._handle_application_state_changed)
            self._app_state_signal_connected = False
        self.config.main_window_geometry = qbytearray_to_bytes(self.saveGeometry())
        if self.isVisible():
            self.config.last_active_window = "main"
        self._save_config()
        super().closeEvent(event)
