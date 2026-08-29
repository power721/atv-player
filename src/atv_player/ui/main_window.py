from __future__ import annotations

import inspect
import json
import platform
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlparse

import httpx
import shiboken6
from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QGuiApplication,
    QKeySequence,
    QMouseEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from atv_player.builtin_tab_overrides import dumps_builtin_tab_overrides_json, parse_builtin_tab_overrides_json
from atv_player.controllers.browse_controller import (
    _map_vod_item,
    build_drive_grouped_sources,
)
from atv_player.controllers.media_detail_controller import MediaDetailIdentity, MediaDetailLookup
from atv_player.controllers.telegram_search_controller import build_detail_playlist
from atv_player.danmaku.direct_parse import DirectParseDanmakuController
from atv_player.diagnostics import SystemInfoEntry, collect_system_info_entries
from atv_player.following_progress import resolve_following_playback_progress
from atv_player.heat import (
    HeatRecommendation,
    has_required_heat_external_id,
    heat_identity_from_vod,
)
from atv_player.log_store import AppLogFilter
from atv_player.models import (
    AppConfig,
    BuiltinTabOverrides,
    FavoriteRecord,
    HistoryRecord,
    OpenPlayerRequest,
    PlaybackDetailFieldAction,
    PlayItem,
    VodItem,
)
from atv_player.paths import app_cache_dir, app_data_dir
from atv_player.player.startup import PlaybackStartupStage
from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog
from atv_player.ui.async_guard import AsyncGuardMixin
from atv_player.ui.browse_page import BrowsePage
from atv_player.ui.classic_home_page import ClassicHomePage, SourceEntry
from atv_player.ui.favorites_page import FavoritesPage
from atv_player.ui.following_detail_page import FollowingDetailPage
from atv_player.ui.following_page import FollowingPage
from atv_player.ui.help_dialog import (
    ShortcutHelpDialog,
    shortcut_entries_for,
    show_shortcut_help_dialog,
)
from atv_player.ui.heat_recommendations_dialog import HeatRecommendationsDialog
from atv_player.ui.history_page import HistoryPage
from atv_player.ui.icon_cache import load_tinted_icon
from atv_player.ui.live_source_manager_dialog import LiveSourceManagerDialog
from atv_player.ui.media_detail_page import MediaDetailPage
from atv_player.ui.media_home_page import (
    MediaHomeCard,
    MediaHomePage,
    MediaHomeSections,
)
from atv_player.ui.player_window import PlayerWindow
from atv_player.ui.plugin_actions import PluginActions
from atv_player.ui.plugin_category_manager_dialog import PluginCategoryManagerDialog
from atv_player.ui.plugin_manager_dialog import PluginManagerDialog
from atv_player.ui.plugin_tab_drawer import PluginTabDrawer
from atv_player.ui.poster_grid_page import FilterPanelExpansionState, PosterGridPage
from atv_player.ui.qt_compat import qbytearray_to_bytes, to_qbytearray
from atv_player.ui.simplified_home_page import SimplifiedHomePage
from atv_player.ui.theme import (
    build_navigation_tabbar_qss,
    build_pill_button_qss,
    build_round_icon_button_qss,
    build_search_line_edit_qss,
    current_resolved_theme,
    current_tokens,
)
from atv_player.ui.window_chrome import ThemedDialogBase, ThemedMainWindowBase


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


class _EmptyYouTubeController(_EmptyDoubanController):
    def build_request(self, vod_id: str):
        raise ValueError(f"没有可播放的项目: {vod_id}")


class _EmptyJellyfinController(_EmptyDoubanController):
    def build_request(self, vod_id: str):
        raise ValueError(f"没有可播放的项目: {vod_id}")


class _EmptyFeiniuController(_EmptyDoubanController):
    def build_request(self, vod_id: str):
        raise ValueError(f"没有可播放的项目: {vod_id}")


class _EmptyFavoritesController:
    def load_page(self, *, page: int, size: int, keyword: str):
        del page, size, keyword
        return [], 0

    def search_items(self, keyword: str, page: int):
        del keyword, page
        return [], 0

    def is_favorited(self, *, source_kind: str, source_key: str, vod_id: str) -> bool:
        del source_kind, source_key, vod_id
        return False

    def add_favorite(self, payload: dict[str, object]) -> None:
        del payload

    def remove_favorite(self, records: list[FavoriteRecord]) -> None:
        del records

    def clear_filtered(self, *, keyword: str) -> None:
        del keyword


class _EmptyFollowingController:
    def load_page(self, *, page: int, size: int, keyword: str, only_updates: bool):
        del page, size, keyword, only_updates
        return [], 0

    def search_items(self, keyword: str, page: int):
        del keyword, page
        return [], 0

    def load_homepage_prompts(self):
        return []

    def clear_homepage_prompt(self, following_id: int) -> None:
        del following_id

    def snooze_prompt(self, following_id: int) -> None:
        del following_id

    def dismiss_prompt_until_next_episode(self, following_id: int) -> None:
        del following_id


class _HistoryGlobalSearchAdapter:
    page_size = 100

    def __init__(self, history_controller) -> None:
        self._history_controller = history_controller

    def search_items(self, keyword: str, page: int):
        return self._history_controller.load_page(page=page, size=self.page_size, keyword=keyword)


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
    "guangyapan.com",
)
_DIRECT_PARSE_DETAIL_API = "https://dmku.hls.one/"
_HOTKEY_360_API = "http://api.xcvts.cn/api/hotlist/360so_juhe"
_HOTKEY_TENCENT_API = "https://pbaccess.video.qq.com/trpc.videosearch.hot_rank.HotRankServantHttp/HotRankHttp"
_HOTKEY_IQIYI_API = "https://mesh.if.iqiyi.com/portal/lw/search/keywords/hotList"
_SUGGESTION_360_API = "https://sug.so.360.cn/suggest"
_GLOBAL_SEARCH_HISTORY_LIMIT = 50
_GLOBAL_SEARCH_HISTORY_DISPLAY_LIMIT = 10
_HEAT_RECOMMENDATION_DISPLAY_LIMIT = 30
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
_CUSTOM_MAIN_WINDOW_GEOMETRY_PREFIX = b"main-window-geometry-v2:"


def _available_screen_geometries() -> list[QRect]:
    geometries: list[QRect] = []
    app = QApplication.instance()
    if app is None:
        return geometries
    for screen in app.screens():
        geometry = screen.availableGeometry()
        if geometry.isValid() and geometry.width() > 0 and geometry.height() > 0:
            geometries.append(geometry)
    return geometries


def _distance_squared_to_rect(point: QPoint, rect: QRect) -> int:
    if rect.left() <= point.x() <= rect.right():
        dx = 0
    else:
        dx = min(abs(point.x() - rect.left()), abs(point.x() - rect.right()))
    if rect.top() <= point.y() <= rect.bottom():
        dy = 0
    else:
        dy = min(abs(point.y() - rect.top()), abs(point.y() - rect.bottom()))
    return dx * dx + dy * dy


def _fit_rect_within_available_screens(rect: QRect, screen_geometries: Iterable[QRect]) -> QRect:
    if not rect.isValid() or rect.width() <= 0 or rect.height() <= 0:
        return rect

    screens = [
        QRect(screen)
        for screen in screen_geometries
        if screen.isValid() and screen.width() > 0 and screen.height() > 0
    ]
    if not screens:
        return rect

    center = rect.center()
    best_screen = screens[0]
    best_key: tuple[int, int, int] | None = None
    for index, screen in enumerate(screens):
        intersection = rect.intersected(screen)
        intersection_area = max(0, intersection.width()) * max(0, intersection.height())
        distance = _distance_squared_to_rect(center, screen)
        key = (-intersection_area, distance, index)
        if best_key is None or key < best_key:
            best_key = key
            best_screen = screen

    width = min(max(1, rect.width()), best_screen.width())
    height = min(max(1, rect.height()), best_screen.height())
    min_x = best_screen.x()
    min_y = best_screen.y()
    max_x = best_screen.x() + best_screen.width() - width
    max_y = best_screen.y() + best_screen.height() - height
    x = min(max(rect.x(), min_x), max_x)
    y = min(max(rect.y(), min_y), max_y)
    return QRect(x, y, width, height)


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


def load_direct_parse_detail(url: str) -> dict[str, Any]:
    response = httpx.get(
        _DIRECT_PARSE_DETAIL_API,
        params={"ac": "list", "url": url},
        timeout=10.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def load_360_hot_searches(hot_type: str = _DEFAULT_GLOBAL_SEARCH_HOT_TYPE) -> list[dict[str, str]]:
    response = httpx.get(
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
    response = httpx.post(
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
    response = httpx.get(
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
    response = httpx.get(
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

    def load_folder_items(self, vod_id: str): ...

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
    heat_recommendations_loaded = Signal(int, object)


class _MediaDetailSeasonSignals(QObject):
    succeeded = Signal(int, object, int)
    failed = Signal(int, int, str)


class _MediaDetailLoadSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)


class _HelpPayloadSignals(QObject):
    loaded = Signal(int, object, str, str)


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
    heat_item_clicked = Signal(object)
    clear_history_requested = Signal()
    delete_history_requested = Signal(str)
    hot_source_changed = Signal(str)
    hot_tab_changed = Signal(str)
    HISTORY_ITEM_HEIGHT = 40
    HOT_ITEM_HEIGHT = 48

    @staticmethod
    def _container_qss() -> str:
        tokens = current_tokens()
        return f"""
        QWidget#globalSearchPopupContainer {{
            background: {tokens.window_bg};
            border: 1px solid {tokens.border_subtle};
            border-radius: 0;
            color: {tokens.text_primary};
        }}
        """

    @staticmethod
    def _divider_qss() -> str:
        tokens = current_tokens()
        return f"QFrame {{ color: {tokens.border_subtle}; background: {tokens.border_subtle}; min-width: 1px; }}"

    @staticmethod
    def _section_title_qss() -> str:
        tokens = current_tokens()
        return f"""
        QLabel {{
            color: {tokens.text_secondary};
            font-size: 12px;
            font-weight: 600;
            letter-spacing: 1px;
        }}
        """

    @staticmethod
    def _action_button_qss() -> str:
        tokens = current_tokens()
        return f"""
        QPushButton {{
            color: {tokens.text_secondary};
            font-size: 12px;
            border: none;
            background: transparent;
            padding: 0 4px;
        }}
        QPushButton:hover {{
            color: {tokens.accent_hover};
        }}
        QPushButton:disabled {{
            color: {tokens.button_disabled_text};
        }}
        """

    @staticmethod
    def _empty_label_qss() -> str:
        tokens = current_tokens()
        return f"QLabel {{ color: {tokens.text_secondary}; font-size: 12px; }}"

    @staticmethod
    def _history_row_qss() -> str:
        tokens = current_tokens()
        return f"""
        QWidget {{
            background: transparent;
        }}
        QWidget:hover {{
            background: {tokens.panel_alt_bg};
        }}
        """

    @staticmethod
    def _history_button_qss() -> str:
        tokens = current_tokens()
        return f"""
        QPushButton {{
            text-align: left;
            color: {tokens.text_primary};
            font-size: 13px;
            border: none;
            background: transparent;
            padding: 0;
        }}
        """

    @staticmethod
    def _hot_tab_qss() -> str:
        tokens = current_tokens()
        return f"""
        QTabBar::tab {{
            background: transparent;
            color: {tokens.text_secondary};
            padding: 8px 12px;
            margin-right: 6px;
            border: none;
        }}
        QTabBar::tab:selected {{
            background: {tokens.panel_alt_bg};
            color: {tokens.accent};
            font-weight: 600;
        }}
        """

    @staticmethod
    def _hot_row_qss() -> str:
        tokens = current_tokens()
        return f"""
        QWidget {{
            background: transparent;
        }}
        QWidget:hover {{
            background: {tokens.panel_alt_bg};
        }}
        """

    @staticmethod
    def _hot_rank_qss() -> str:
        tokens = current_tokens()
        return f"QLabel {{ color: {tokens.accent}; font-weight: 700; font-size: 12px; }}"

    @staticmethod
    def _hot_button_qss() -> str:
        tokens = current_tokens()
        return f"""
        QPushButton {{
            text-align: left;
            color: {tokens.text_primary};
            font-size: 14px;
            font-weight: 600;
            border: none;
            background: transparent;
            padding: 0;
        }}
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
        self._heat_item_buttons: dict[str, QPushButton] = {}
        self._history_delete_buttons: dict[str, QPushButton] = {}
        self._hot_source_types: list[str] = []
        self._hot_tab_types: list[str] = []
        self._hot_item_texts: list[str] = []
        self._heat_item_texts: list[str] = []
        self._heat_items_by_title: dict[str, object] = {}
        self._hot_rank_labels: list[QLabel] = []
        self._heat_title_label: QLabel | None = None

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._container = QWidget(self)
        self._container.setObjectName("globalSearchPopupContainer")
        self._container.setStyleSheet(self._container_qss())
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
        separator.setStyleSheet(self._divider_qss())
        self._container_layout.addWidget(separator)

        self._hot_panel = QWidget(self._container)
        self._hot_layout = QVBoxLayout(self._hot_panel)
        self._hot_layout.setContentsMargins(0, 0, 0, 0)
        self._hot_layout.setSpacing(0)
        self._container_layout.addWidget(self._hot_panel, 1)

        self._build_history_panel()
        self._build_hot_panel()

    def _apply_theme(self) -> None:
        self._container.setStyleSheet(self._container_qss())
        divider_item = self._container_layout.itemAt(1)
        divider_widget = divider_item.widget() if divider_item is not None else None
        if isinstance(divider_widget, QFrame):
            divider_widget.setStyleSheet(self._divider_qss())
        if self._history_title_label is not None:
            self._history_title_label.setStyleSheet(self._section_title_qss())
        if self._hot_title_label is not None:
            self._hot_title_label.setStyleSheet(self._section_title_qss())
        if self._heat_title_label is not None:
            self._heat_title_label.setStyleSheet(self._section_title_qss())
        if self.clear_history_button is not None:
            self.clear_history_button.setStyleSheet(self._action_button_qss())
        self.hot_source_tab_bar.setStyleSheet(self._hot_tab_qss())
        self.hot_tab_bar.setStyleSheet(self._hot_tab_qss())
        for row in self._history_item_rows.values():
            row.setStyleSheet(self._history_row_qss())
        for button in self._history_item_buttons.values():
            button.setStyleSheet(self._history_button_qss())
        for button in self._history_delete_buttons.values():
            button.setStyleSheet(self._action_button_qss())
        for label in self._hot_rank_labels:
            label.setStyleSheet(self._hot_rank_qss())
        for button in self._hot_item_buttons.values():
            button.setStyleSheet(self._hot_button_qss())
        for button in self._heat_item_buttons.values():
            button.setStyleSheet(self._hot_button_qss())

    def history_item_texts(self) -> list[str]:
        return [button.text() for button in self._history_item_buttons.values()]

    def hot_item_texts(self) -> list[str]:
        return list(self._hot_item_texts)

    def heat_item_texts(self) -> list[str]:
        return list(self._heat_item_texts)

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

    def heat_item_button(self, text: str) -> QPushButton:
        return self._heat_item_buttons[text]

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

    def set_heat_items(self, items: Iterable[object]) -> None:
        self._clear_layout(self._heat_items_layout)
        self._heat_item_buttons = {}
        self._heat_item_texts = []
        self._heat_items_by_title = {}
        item_exists = False
        for item in list(items)[:6]:
            title = str(getattr(item, "title", "") or "").strip()
            if not title:
                continue
            item_exists = True
            self._add_heat_item(item, title)
        self._heat_title_label.setVisible(item_exists)
        self._heat_items_widget.setVisible(item_exists)
        self.adjustSize()

    def _build_history_panel(self) -> None:
        title_widget = QWidget(self._history_panel)
        title_layout = QHBoxLayout(title_widget)
        title_layout.setContentsMargins(16, 14, 16, 10)
        title = QLabel("搜索历史", title_widget)
        title.setStyleSheet(self._section_title_qss())
        self._history_title_label = title
        title_layout.addWidget(title)
        title_layout.addStretch(1)
        clear_button = QPushButton("清空", title_widget)
        clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_button.setFlat(True)
        clear_button.setStyleSheet(self._action_button_qss())
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
        heat_title = QLabel("大家在看", self._hot_panel)
        heat_title.setContentsMargins(16, 14, 16, 8)
        heat_title.setStyleSheet(self._section_title_qss())
        heat_title.hide()
        self._heat_title_label = heat_title
        self._hot_layout.addWidget(heat_title)
        self._heat_items_widget = QWidget(self._hot_panel)
        self._heat_items_layout = QVBoxLayout(self._heat_items_widget)
        self._heat_items_layout.setContentsMargins(8, 0, 8, 10)
        self._heat_items_layout.setSpacing(0)
        self._heat_items_widget.hide()
        self._hot_layout.addWidget(self._heat_items_widget)

        title = QLabel("热搜", self._hot_panel)
        title.setContentsMargins(16, 14, 16, 10)
        title.setStyleSheet(self._section_title_qss())
        self._hot_title_label = title
        self._hot_layout.addWidget(title)
        self.hot_source_tab_bar.setDocumentMode(True)
        self.hot_source_tab_bar.setExpanding(False)
        self.hot_source_tab_bar.setUsesScrollButtons(True)
        self.hot_source_tab_bar.setDrawBase(False)
        self.hot_source_tab_bar.setElideMode(Qt.TextElideMode.ElideNone)
        self.hot_source_tab_bar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hot_source_tab_bar.setStyleSheet(self._hot_tab_qss())
        self.hot_source_tab_bar.currentChanged.connect(self._handle_hot_source_tab_changed)
        self._hot_layout.addWidget(self.hot_source_tab_bar)
        self.hot_tab_bar.setDocumentMode(True)
        self.hot_tab_bar.setExpanding(False)
        self.hot_tab_bar.setUsesScrollButtons(True)
        self.hot_tab_bar.setDrawBase(False)
        self.hot_tab_bar.setElideMode(Qt.TextElideMode.ElideNone)
        self.hot_tab_bar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hot_tab_bar.setStyleSheet(self._hot_tab_qss())
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
            empty_label.setStyleSheet(self._empty_label_qss())
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
            empty_label.setStyleSheet(self._empty_label_qss())
            self._hot_items_layout.addWidget(empty_label)

    def _add_history_item(self, keyword: str) -> None:
        row = QWidget(self._history_items_widget)
        row.setFixedHeight(self.HISTORY_ITEM_HEIGHT)
        row.setStyleSheet(self._history_row_qss())
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(12, 4, 10, 4)
        row_layout.setSpacing(8)
        item_button = QPushButton(keyword, row)
        item_button.setFixedHeight(self.HISTORY_ITEM_HEIGHT - 8)
        item_button.setCursor(Qt.CursorShape.PointingHandCursor)
        item_button.setFlat(True)
        item_button.setStyleSheet(self._history_button_qss())
        item_button.clicked.connect(lambda checked=False, current_keyword=keyword: self._on_item_clicked(current_keyword))
        delete_button = QPushButton("删除", row)
        delete_button.setFixedHeight(self.HISTORY_ITEM_HEIGHT - 8)
        delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_button.setFlat(True)
        delete_button.setStyleSheet(self._action_button_qss())
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
        row.setStyleSheet(self._hot_row_qss())
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(12, 6, 12, 6)
        row_layout.setSpacing(10)

        rank_label = QLabel(f"{index:02d}", row)
        rank_label.setStyleSheet(self._hot_rank_qss())

        button = QPushButton(text, row)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFlat(True)
        button.setStyleSheet(self._hot_button_qss())
        button.clicked.connect(lambda checked=False, current_query=query: self._on_item_clicked(current_query))

        row_layout.addWidget(rank_label)
        row_layout.addWidget(button, 1)

        self._hot_rank_labels.append(rank_label)
        self._hot_item_buttons[text] = button
        self._hot_item_texts.append(text)
        self._hot_items_layout.addWidget(row)

    def _add_heat_item(self, item: object, title: str) -> None:
        row = QWidget(self._heat_items_widget)
        row.setFixedHeight(self.HOT_ITEM_HEIGHT)
        row.setStyleSheet(self._hot_row_qss())
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(12, 6, 12, 6)
        row_layout.setSpacing(10)

        button = QPushButton(title, row)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFlat(True)
        button.setStyleSheet(self._hot_button_qss())
        reason = str(getattr(item, "reason", "") or "").strip()
        if reason:
            button.setToolTip(reason)
        button.clicked.connect(lambda checked=False, current_item=item: self._on_heat_item_clicked(current_item))

        row_layout.addWidget(button, 1)
        self._heat_item_buttons[title] = button
        self._heat_item_texts.append(title)
        self._heat_items_by_title[title] = item
        self._heat_items_layout.addWidget(row)

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

    def _on_heat_item_clicked(self, item: object) -> None:
        self.hide()
        self.heat_item_clicked.emit(item)

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
    folder_id: str = ""
    folder_page_loader: Any | None = None
    page_number: int = 1


@dataclass(slots=True)
class _TabDefinition:
    key: str
    title: str
    page: QWidget
    search_controller: Any | None = None
    global_search_only: bool = False


@dataclass(slots=True)
class _BuiltinTabDefinition:
    key: str
    title: str
    page: QWidget
    search_controller: Any | None = None
    global_search_only: bool = False
    trailing: bool = False


@dataclass(slots=True)
class _GlobalSearchResult:
    key: str
    title: str
    page: QWidget
    items: list[Any]
    total_count: int
    page_count: int
    page_number: int


def _is_valid_qt_widget(widget: QWidget | None) -> bool:
    return widget is not None and shiboken6.isValid(widget)


class _NavigationTabs(QWidget):
    currentChanged = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.tab_bar = QTabBar(self)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setMovable(False)
        self.tab_bar.setDocumentMode(True)
        self.tab_bar.setUsesScrollButtons(False)
        self.tab_bar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.plugin_overflow_button = QPushButton("更多", self)
        self.plugin_overflow_button.setCheckable(True)
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
        self.nav_row_widget = QWidget(self)
        self.nav_row_widget.setLayout(nav_row)
        layout.addWidget(self.nav_row_widget)
        layout.addWidget(self.content_stack, 1)
        self.tab_bar.currentChanged.connect(self._handle_tab_bar_changed)

    def _handle_tab_bar_changed(self, index: int) -> None:
        widget = self.widget(index)
        if widget is None:
            if index < 0 and not self.signalsBlocked():
                self.currentChanged.emit(index)
            return
        self.content_stack.setCurrentWidget(widget)
        if not self.signalsBlocked():
            self.currentChanged.emit(index)

    def clear(self) -> None:
        while self.tab_bar.count() > 0:
            self.tab_bar.removeTab(self.tab_bar.count() - 1)
        self._visible_widgets = []

    def ensure_widget(self, widget: QWidget) -> None:
        if not _is_valid_qt_widget(widget):
            return
        if self.content_stack.indexOf(widget) < 0:
            self.content_stack.addWidget(widget)

    def addTab(self, widget: QWidget, title: str) -> int:
        if not _is_valid_qt_widget(widget):
            return -1
        self.ensure_widget(widget)
        self._visible_widgets.append(widget)
        return self.tab_bar.addTab(title)

    def count(self) -> int:
        return self.tab_bar.count()

    def tabText(self, index: int) -> str:
        return self.tab_bar.tabText(index)

    def currentWidget(self) -> QWidget | None:
        widget = self.content_stack.currentWidget()
        return widget if _is_valid_qt_widget(widget) else None

    def currentIndex(self) -> int:
        current_widget = self.currentWidget()
        if current_widget is None:
            return -1
        return self.indexOf(current_widget)

    def widget(self, index: int) -> QWidget | None:
        if 0 <= index < len(self._visible_widgets):
            widget = self._visible_widgets[index]
            return widget if _is_valid_qt_widget(widget) else None
        return None

    def indexOf(self, widget: QWidget) -> int:
        if not _is_valid_qt_widget(widget):
            return -1
        for index, visible_widget in enumerate(self._visible_widgets):
            if visible_widget is widget and _is_valid_qt_widget(visible_widget):
                return index
        return -1

    def setCurrentIndex(self, index: int) -> None:
        widget = self.widget(index)
        if widget is None:
            return
        self.tab_bar.setCurrentIndex(index)
        self.content_stack.setCurrentWidget(widget)

    def setCurrentWidget(self, widget: QWidget) -> None:
        if not _is_valid_qt_widget(widget):
            return
        self.ensure_widget(widget)
        previous_widget = self.currentWidget()
        index = self.indexOf(widget)
        if index >= 0:
            previous_index = self.tab_bar.currentIndex()
            self.tab_bar.setCurrentIndex(index)
            self.content_stack.setCurrentWidget(widget)
            if previous_index == index and previous_widget is not widget and not self.signalsBlocked():
                self.currentChanged.emit(index)
            return
        previous_index = self.tab_bar.currentIndex()
        self.tab_bar.blockSignals(True)
        self.tab_bar.setCurrentIndex(-1)
        self.tab_bar.blockSignals(False)
        self.content_stack.setCurrentWidget(widget)
        if not self.signalsBlocked() and (previous_index != -1 or previous_widget is not widget):
            self.currentChanged.emit(-1)

    def blockSignals(self, block: bool) -> bool:
        previous = super().blockSignals(block)
        self.tab_bar.blockSignals(block)
        return previous

    def setNavigationVisible(self, visible: bool) -> None:
        self.nav_row_widget.setVisible(visible)

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


class MainWindow(ThemedMainWindowBase, AsyncGuardMixin):
    logout_requested = Signal()
    _ICONS_DIR = Path(__file__).resolve().parent.parent / "icons"
    _SEARCH_ICON_PATH = _ICONS_DIR / "search.svg"
    _SEARCH_POPUP_ICON_PATH = _ICONS_DIR / "rank.svg"
    _HEAT_RECOMMENDATIONS_ICON_PATH = _ICONS_DIR / "fire.svg"
    _HOME_ICON_PATH = _ICONS_DIR / "home.svg"
    _BROWSE_ICON_PATH = _ICONS_DIR / "folder.svg"
    _FAVORITES_ICON_PATH = _ICONS_DIR / "favorite.svg"
    _FOLLOWING_ICON_PATH = _ICONS_DIR / "following.svg"
    _HISTORY_ICON_PATH = _ICONS_DIR / "history.svg"
    _PLUGIN_MANAGER_ICON_PATH = _ICONS_DIR / "plugin.svg"
    _LIVE_SOURCE_MANAGER_ICON_PATH = _ICONS_DIR / "live-source.svg"
    _ADVANCED_SETTINGS_ICON_PATH = _ICONS_DIR / "sliders.svg"
    _LOGOUT_ICON_PATH = _ICONS_DIR / "logout.svg"

    def __init__(
            self,
            browse_controller,
            history_controller,
            player_controller,
            config,
            favorites_controller=None,
            following_controller=None,
            following_update_service=None,
            app_log_service=None,
            save_config=None,
            apply_theme=None,
            douban_controller=None,
            global_catalog_controller=None,
            telegram_controller=None,
            telegram_channel_controller=None,
            bilibili_controller=None,
            youtube_controller=None,
            live_controller=None,
            live_source_manager=None,
            emby_controller=None,
            jellyfin_controller=None,
            feiniu_controller=None,
            pansou_controller=None,
            msub_controller=None,
            spider_plugins=None,
            plugin_loader_task=None,
            plugin_manager=None,
            drive_detail_loader=None,
            drive_resolver=None,
            drive_files_loader=None,
            offline_download_detail_loader=None,
            direct_parse_detail_loader=None,
            direct_parse_danmaku_loader=None,
            direct_parse_playback_history_loader=None,
            direct_parse_playback_history_saver=None,
            default_video_cover_loader=None,
            global_search_hotkey_loader=None,
            global_search_suggestion_loader=None,
            youtube_category_text_loader=None,
            show_telegram_channel_tab: bool = False,
            show_bilibili_tab: bool = False,
            show_youtube_tab: bool = False,
            show_emby_tab: bool = True,
            show_jellyfin_tab: bool = True,
            show_feiniu_tab: bool = True,
            m3u8_ad_filter=None,
            playback_parser_service=None,
            yt_dlp_service=None,
            smart_search_controller=None,
            heat_controller=None,
            media_detail_controller=None,
            metadata_hydrator_factory=None,
            metadata_scrape_service_factory=None,
            subtitle_search_service=None,
            danmaku_controller_factory=None,
            episode_title_enhancer_factory=None,
            metadata_binding_repository=None,
            episode_title_override_repository=None,
            danmaku_preference_store=None,
    ) -> None:
        super().__init__(title="alist-tvbox Desktop Player", resizable=True)
        self._init_async_guard()
        self._save_config = save_config or (lambda: None)
        self._apply_application_theme = apply_theme or (lambda: None)
        self._app_log_service = app_log_service
        self._following_update_service = following_update_service
        self._m3u8_ad_filter = m3u8_ad_filter
        self._playback_parser_service = playback_parser_service
        self._yt_dlp_service = yt_dlp_service
        self._smart_search_controller = smart_search_controller
        self._heat_controller = heat_controller
        self._media_detail_controller = media_detail_controller
        self._metadata_hydrator_factory = metadata_hydrator_factory
        self._metadata_scrape_service_factory = metadata_scrape_service_factory
        self._subtitle_search_service = subtitle_search_service
        self._danmaku_controller_factory = danmaku_controller_factory
        self._episode_title_enhancer_factory = episode_title_enhancer_factory
        self._metadata_binding_repository = metadata_binding_repository
        self._episode_title_override_repository = episode_title_override_repository
        self._danmaku_preference_store = danmaku_preference_store
        self.config = config
        self._plugin_definitions = list(spider_plugins or [])
        self._plugin_loader_task = plugin_loader_task
        self._plugin_manager = plugin_manager
        self._drive_detail_loader = drive_detail_loader
        self._drive_resolver = drive_resolver
        self._drive_files_loader = drive_files_loader
        self._offline_download_detail_loader = offline_download_detail_loader
        self._direct_parse_detail_loader = direct_parse_detail_loader
        self._direct_parse_danmaku_loader = direct_parse_danmaku_loader
        self._direct_parse_playback_history_loader = direct_parse_playback_history_loader
        self._direct_parse_playback_history_saver = direct_parse_playback_history_saver
        self._default_video_cover_loader = default_video_cover_loader
        self._global_search_hotkey_loader = global_search_hotkey_loader or load_global_search_hotkey_payload
        self._global_search_suggestion_loader = global_search_suggestion_loader or load_360_search_suggestions
        self._youtube_category_text_loader = youtube_category_text_loader
        self._live_source_manager = live_source_manager
        self._plugin_pages: list[tuple[PosterGridPage, _PluginController, str]] = []
        self._static_tab_definitions: list[_TabDefinition] = []
        self._trailing_tab_definitions: list[_TabDefinition] = []
        self._plugin_tab_definitions: list[_TabDefinition] = []
        self._hidden_plugin_tab_definitions: list[_TabDefinition] = []
        self._builtin_tab_definitions: list[_BuiltinTabDefinition] = []
        self._defer_navigation_refresh = True
        self._classic_startup_mode = (getattr(config, "home_mode", "browse") or "browse") == "classic"
        self._startup_plugin_load_started = False
        self._startup_plugin_load_request_id = 0
        self._startup_plugin_load_state = (
            "loading" if callable(plugin_loader_task) and not self._classic_startup_mode else "idle"
        )
        self._startup_plugin_load_error = ""
        self._startup_selected_category_id = str(getattr(config, "last_selected_category_id", "") or "").strip()
        selected_tab = str(getattr(config, "last_selected_tab", "") or "")
        self._startup_pending_tab_restore_key = selected_tab
        self._startup_plugin_pending_tab_restore_key = (
            selected_tab
            if callable(plugin_loader_task) and not self._classic_startup_mode and selected_tab.startswith("plugin:")
            else ""
        )
        self._startup_plugin_pending_player_restore = False
        self._active_widget: QWidget | None = None
        self._filter_panel_state = FilterPanelExpansionState()
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
        self.heat_recommendations_button = QPushButton("")
        self.global_search_clear_button = QPushButton("清空")
        self.global_search_status_label = QLabel("")
        self._global_search_popup: GlobalSearchPopup | None = None
        self._heat_recommendations_dialog: HeatRecommendationsDialog | None = None
        self.startup_plugin_status_label = QLabel("")
        self.startup_plugin_retry_button = QPushButton("重试加载插件")
        self.startup_plugin_retry_button.hide()
        self.home_button = QPushButton("")
        self.browse_button = QPushButton("")
        self.favorites_button = QPushButton("")
        self.following_button = QPushButton("")
        self.history_button = QPushButton("")
        self.plugin_manager_button = QPushButton("")
        self.live_source_manager_button = QPushButton("")
        self.advanced_settings_button = QPushButton("")
        self.logout_button = QPushButton("")
        self.header_action_separators = [self._create_header_action_separator() for _ in range(2)]
        self.browse_page = BrowsePage(browse_controller, config=config, save_config=self._save_config)
        self.douban_page = PosterGridPage(
            douban_controller or _EmptyDoubanController(),
            initial_category_id=self._initial_category_id_for_tab("douban"),
            filter_panel_state=self._filter_panel_state,
        )
        self.global_catalog_page = PosterGridPage(
            global_catalog_controller or _EmptyDoubanController(),
            click_action="open",
            initial_category_id=self._initial_category_id_for_tab("global_catalog"),
            filter_panel_state=self._filter_panel_state,
        )
        self.telegram_page = PosterGridPage(
            telegram_controller or _EmptyTelegramController(),
            click_action="open",
            search_enabled=True,
            search_drive_filter_enabled=True,
            initial_category_id=self._initial_category_id_for_tab("telegram"),
            filter_panel_state=self._filter_panel_state,
        )
        self.telegram_channel_page = None
        if show_telegram_channel_tab:
            self.telegram_channel_page = PosterGridPage(
                telegram_channel_controller or _EmptyTelegramController(),
                click_action="open",
                search_enabled=True,
                search_drive_filter_enabled=True,
                initial_category_id=self._initial_category_id_for_tab("telegram_channel"),
                filter_panel_state=self._filter_panel_state,
            )
        self.bilibili_page = None
        if show_bilibili_tab:
            self.bilibili_page = PosterGridPage(
                bilibili_controller or _EmptyBilibiliController(),
                click_action="open",
                search_enabled=True,
                folder_navigation_enabled=True,
                initial_category_id=self._initial_category_id_for_tab("bilibili"),
                filter_panel_state=self._filter_panel_state,
            )
        self.youtube_page = None
        if show_youtube_tab:
            self.youtube_page = PosterGridPage(
                youtube_controller or _EmptyYouTubeController(),
                click_action="open",
                search_enabled=True,
                initial_category_id=self._initial_category_id_for_tab("youtube"),
                filter_panel_state=self._filter_panel_state,
            )
        self.live_page = PosterGridPage(
            live_controller or _EmptyLiveController(),
            click_action="open",
            folder_navigation_enabled=True,
            initial_category_id=self._initial_category_id_for_tab("live"),
            filter_panel_state=self._filter_panel_state,
        )
        self.emby_page = None
        if show_emby_tab:
            self.emby_page = PosterGridPage(
                emby_controller or _EmptyEmbyController(),
                click_action="open",
                search_enabled=True,
                folder_navigation_enabled=True,
                initial_category_id=self._initial_category_id_for_tab("emby"),
                filter_panel_state=self._filter_panel_state,
            )
        self.jellyfin_page = None
        if show_jellyfin_tab:
            self.jellyfin_page = PosterGridPage(
                jellyfin_controller or _EmptyJellyfinController(),
                click_action="open",
                search_enabled=True,
                folder_navigation_enabled=True,
                initial_category_id=self._initial_category_id_for_tab("jellyfin"),
                filter_panel_state=self._filter_panel_state,
            )
        self.feiniu_page = None
        if show_feiniu_tab:
            self.feiniu_page = PosterGridPage(
                feiniu_controller or _EmptyFeiniuController(),
                click_action="open",
                search_enabled=True,
                folder_navigation_enabled=True,
                initial_category_id=self._initial_category_id_for_tab("feiniu"),
                filter_panel_state=self._filter_panel_state,
            )
        self._favorites_controller = favorites_controller or _EmptyFavoritesController()
        self._following_controller = following_controller or _EmptyFollowingController()
        self._history_controller = history_controller
        self.favorites_page = FavoritesPage(
            self._favorites_controller,
            source_label_resolver=self._favorite_record_source_label,
        )
        self.following_page = FollowingPage(self._following_controller)
        self.following_detail_page = FollowingDetailPage(
            self._following_controller,
            config=self.config,
            save_config=self._save_config,
        )
        self.media_detail_page = MediaDetailPage(
            config=self.config,
            save_config=self._save_config,
        )
        self.history_page = HistoryPage(history_controller)
        self.global_history_page = HistoryPage(history_controller)
        self._global_history_search_adapter = (
            _HistoryGlobalSearchAdapter(history_controller)
            if hasattr(history_controller, "load_page")
            else None
        )
        self.pansou_page = None
        if pansou_controller is not None:
            self.pansou_page = PosterGridPage(
                pansou_controller,
                click_action="open",
                search_drive_filter_enabled=True,
                initial_category_id=self._initial_category_id_for_tab("pansou"),
                filter_panel_state=self._filter_panel_state,
            )
        self.browse_controller = browse_controller
        self.telegram_controller = telegram_controller or _EmptyTelegramController()
        self.telegram_channel_controller = telegram_channel_controller or _EmptyTelegramController()
        self._skip_next_telegram_open_request_vod_id = ""
        self.bilibili_controller = bilibili_controller or _EmptyBilibiliController()
        self.youtube_controller = youtube_controller or _EmptyYouTubeController()
        self._youtube_open_request_id = 0
        self._youtube_open_request_vod_id = ""
        self.live_controller = live_controller or _EmptyLiveController()
        self.emby_controller = emby_controller or _EmptyEmbyController()
        self.jellyfin_controller = jellyfin_controller or _EmptyJellyfinController()
        self.feiniu_controller = feiniu_controller or _EmptyFeiniuController()
        self.pansou_controller = pansou_controller
        self.msub_controller = msub_controller
        self.player_controller = player_controller
        self.player_window: PlayerWindow | None = None
        self.help_dialog: ShortcutHelpDialog | None = None
        self._following_prompt_dialog: ThemedDialogBase | None = None
        self._following_prompt_detail_button: QPushButton | None = None
        self._following_prompt_search_button: QPushButton | None = None
        self._following_prompt_snooze_button: QPushButton | None = None
        self._following_prompt_dismiss_until_next_button: QPushButton | None = None
        self._following_prompt_close_handled = False
        self._open_request_id = 0
        self._media_request_id = 0
        self._restore_request_id = 0
        self._player_session_request_id = 0
        self._returning_to_main = False
        self._main_window_geometry_before_player = QRect()
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
        self._media_detail_season_request_id = 0
        self._media_detail_season_signals = _MediaDetailSeasonSignals()
        self._connect_async_signal(
            self._media_detail_season_signals.succeeded,
            self._handle_media_detail_season_succeeded,
        )
        self._connect_async_signal(
            self._media_detail_season_signals.failed,
            self._handle_media_detail_season_failed,
        )
        self._media_detail_load_request_id = 0
        self._media_detail_load_signals = _MediaDetailLoadSignals()
        self._connect_async_signal(
            self._media_detail_load_signals.succeeded,
            self._handle_media_detail_load_succeeded,
        )
        self._connect_async_signal(
            self._media_detail_load_signals.failed,
            self._handle_media_detail_load_failed,
        )
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
        self._global_search_restore_plugin_keys: list[str] = []
        self._global_search_popup_signals = _GlobalSearchPopupSignals()
        self._connect_async_signal(self._global_search_popup_signals.hotkeys_loaded, self._handle_global_search_hotkeys_loaded)
        self._connect_async_signal(
            self._global_search_popup_signals.heat_recommendations_loaded,
            self._handle_heat_recommendations_loaded,
        )
        self._help_payload_signals = _HelpPayloadSignals()
        self._connect_async_signal(self._help_payload_signals.loaded, self._handle_help_payload_loaded)
        self._help_payload_request_id = 0
        self._global_search_hotkey_request_id = 0
        self._heat_recommendation_request_id = 0
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
        self._last_normal_geometry = QRect(0, 0, self.width(), self.height())

        tokens = current_tokens()
        self.global_search_container.setFixedWidth(444)
        self.global_search_edit.setPlaceholderText("搜索")
        self.global_search_edit.setClearButtonEnabled(True)
        self.global_search_edit.setStyleSheet(build_search_line_edit_qss(tokens, border_radius=18, min_height=36))
        self.global_search_button.setText("")
        self.global_search_button.setFixedSize(36, 36)
        self.global_search_popup_button.setFixedSize(36, 36)
        self.heat_recommendations_button.setFixedSize(36, 36)
        self.global_search_popup_button.setToolTip("搜索热榜")
        self.global_search_popup_button.setAccessibleName("搜索热榜")
        self.heat_recommendations_button.setToolTip("大家在看")
        self.heat_recommendations_button.setAccessibleName("大家在看")
        self.heat_recommendations_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._configure_header_icon_button(self.home_button, "首页")
        self.home_button.setIconSize(QSize(22, 22))
        self._configure_header_icon_button(self.browse_button, "文件浏览")
        self._configure_header_icon_button(self.favorites_button, "我的收藏")
        self._configure_header_icon_button(self.following_button, "我的追更")
        self._configure_header_icon_button(self.history_button, "播放记录")
        self._configure_header_icon_button(self.plugin_manager_button, "源管理")
        self._configure_header_icon_button(self.live_source_manager_button, "直播源管理")
        self._configure_header_icon_button(self.advanced_settings_button, "高级设置")
        self._configure_header_icon_button(self.logout_button, "退出登录")
        self._apply_global_search_button_theme()
        self.global_search_status_label.setWordWrap(True)
        self.global_search_clear_button.setEnabled(False)
        self.global_search_clear_button.hide()

        self._static_tab_definitions = [
            _TabDefinition("douban", "豆瓣电影", self.douban_page),
            _TabDefinition("global_catalog", "全球片单", self.global_catalog_page),
            _TabDefinition("telegram", "电报影视", self.telegram_page, self.telegram_controller),
        ]
        if self.telegram_channel_page is not None:
            self._static_tab_definitions.append(
                _TabDefinition("telegram_channel", "电报频道", self.telegram_channel_page, self.telegram_channel_controller)
            )
        if self.bilibili_page is not None:
            self._static_tab_definitions.append(
                _TabDefinition("bilibili", "B站", self.bilibili_page, self.bilibili_controller)
            )
        if self.youtube_page is not None:
            self._static_tab_definitions.append(
                _TabDefinition("youtube", "YouTube", self.youtube_page, self.youtube_controller)
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
        if self._smart_search_controller is not None:
            self._static_tab_definitions.append(
                _TabDefinition(
                    "smart:search",
                    "智能匹配",
                    PosterGridPage(
                        self._smart_search_controller,
                        click_action="open",
                        search_enabled=False,
                        filter_panel_state=self._filter_panel_state,
                    ),
                    self._smart_search_controller,
                    global_search_only=True,
                )
            )
        if self._global_history_search_adapter is not None:
            self._static_tab_definitions.append(
                _TabDefinition(
                    "history:global",
                    "播放记录",
                    self.global_history_page,
                    self._global_history_search_adapter,
                    global_search_only=True,
                )
            )
        self._trailing_tab_definitions = [
            _TabDefinition("browse", "文件浏览", self.browse_page),
            _TabDefinition("favorites", "我的收藏", self.favorites_page, self._favorites_controller),
            _TabDefinition("following", "我的追更", self.following_page, self._following_controller),
            _TabDefinition("history", "播放记录", self.history_page),
        ]
        self._builtin_tab_definitions = self._build_builtin_tab_definitions()
        self._rebuild_spider_plugin_tabs()
        self.logout_button.clicked.connect(self.logout_requested.emit)
        self.plugin_overflow_button.clicked.connect(self._toggle_plugin_overflow_drawer)
        self.nav_tabs.tab_bar.customContextMenuRequested.connect(self._handle_tab_context_menu_requested)
        self._plugin_overflow_drawer.plugin_selected.connect(self._handle_hidden_plugin_selected)
        self._plugin_overflow_drawer.plugin_context_requested.connect(self._open_plugin_context_menu)
        self._plugin_overflow_drawer.close_requested.connect(self._close_plugin_overflow_drawer)
        self.startup_plugin_retry_button.clicked.connect(self._retry_startup_plugin_load)
        self.browse_button.clicked.connect(lambda: self._open_builtin_page_from_header("browse"))
        self.favorites_button.clicked.connect(lambda: self._open_builtin_page_from_header("favorites"))
        self.following_button.clicked.connect(lambda: self._open_builtin_page_from_header("following"))
        self.history_button.clicked.connect(lambda: self._open_builtin_page_from_header("history"))
        self.plugin_manager_button.clicked.connect(self._open_plugin_manager)
        self.live_source_manager_button.clicked.connect(self._open_live_source_manager)
        self.advanced_settings_button.clicked.connect(self._open_advanced_settings)
        self.global_search_button.clicked.connect(self._start_global_search)
        self.global_search_popup_button.clicked.connect(self._toggle_global_search_popup)
        self.heat_recommendations_button.clicked.connect(self._show_heat_recommendations_dialog)
        self.global_search_clear_button.clicked.connect(self._clear_global_search)
        self.global_search_edit.returnPressed.connect(self._start_global_search)
        self.global_search_edit.textChanged.connect(self._handle_global_search_text_changed)
        self.global_search_edit.escape_pressed.connect(self._hide_global_search_popup)
        self.home_button.clicked.connect(self._return_to_configured_home)
        search_layout = QHBoxLayout(self.global_search_container)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(8)
        search_layout.addWidget(self.global_search_edit, 1)
        search_layout.addWidget(self.global_search_button)
        search_layout.addWidget(self.global_search_popup_button)
        search_layout.addWidget(self.heat_recommendations_button)
        self.header_layout = QHBoxLayout()
        self.header_leading_spacer = QSpacerItem(
            0,
            0,
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        self.header_center_spacer = QSpacerItem(
            0,
            0,
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        self.header_layout.addItem(self.header_leading_spacer)
        self.header_layout.addWidget(self.global_search_container)
        self.header_layout.addItem(self.header_center_spacer)
        self.header_layout.addWidget(self.startup_plugin_status_label)
        self.header_layout.addWidget(self.startup_plugin_retry_button)
        self.header_layout.addWidget(self.home_button)
        self.header_layout.addWidget(self.browse_button)
        self.header_layout.addWidget(self.favorites_button)
        self.header_layout.addWidget(self.following_button)
        self.header_layout.addWidget(self.history_button)
        self.header_layout.addWidget(self.header_action_separators[0])
        self.header_layout.addWidget(self.plugin_manager_button)
        self.header_layout.addWidget(self.live_source_manager_button)
        self.header_layout.addWidget(self.advanced_settings_button)
        self.header_layout.addWidget(self.header_action_separators[1])
        self.header_layout.addWidget(self.logout_button)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.addLayout(self.header_layout)
        container_layout.addWidget(self.global_search_status_label)
        self._home_stack = QStackedWidget()
        self._home_stack.addWidget(self.nav_tabs)
        container_layout.addWidget(self._home_stack)
        self.content_layout().addWidget(container)
        if self.config.main_window_geometry:
            self._restore_saved_geometry(apply_maximized=True)

        self._defer_navigation_refresh = False
        self.nav_tabs.ensure_widget(self.media_detail_page)
        self.nav_tabs.currentChanged.connect(self._handle_tab_changed)
        self.browse_page.open_requested.connect(self.open_player)
        self.favorites_page.open_detail_requested.connect(self.open_favorite_detail)
        self.favorites_page.global_search_requested.connect(self._handle_favorite_global_search)
        self.following_page.open_detail_requested.connect(self.open_following_detail)
        self.following_detail_page.back_requested.connect(
            self._return_to_following_page
        )
        self.following_detail_page.continue_play_requested.connect(self.open_following_bound_source)
        self.following_detail_page.search_play_requested.connect(self.search_play_for_following)
        self.following_detail_page.unfollow_requested.connect(self._unfollow_from_detail)
        self.following_detail_page.related_global_search_requested.connect(self._handle_favorite_global_search)
        self.following_detail_page.related_media_detail_requested.connect(self.open_media_detail_from_related_request)
        self.media_detail_page.back_requested.connect(self._return_from_media_detail)
        self.media_detail_page.search_play_requested.connect(self.search_play_for_media_detail)
        self.media_detail_page.add_following_requested.connect(self.add_following_from_media_detail)
        self.media_detail_page.refresh_metadata_requested.connect(self.refresh_media_detail_metadata)
        self.media_detail_page.related_open_requested.connect(self.open_media_detail_from_identity)
        self.media_detail_page.season_requested.connect(self.load_media_detail_season)
        self.history_page.open_detail_requested.connect(self.open_history_detail)
        self.global_history_page.open_detail_requested.connect(self.open_history_detail)
        self.history_page.global_search_requested.connect(self._handle_favorite_global_search)
        self.global_history_page.global_search_requested.connect(self._handle_favorite_global_search)
        self.history_page.favorite_requested.connect(self._handle_history_context_favorite)
        self.global_history_page.favorite_requested.connect(self._handle_history_context_favorite)
        if self._following_update_service is not None:
            update_finished = getattr(self._following_update_service, "update_finished", None)
            if update_finished is not None:
                update_finished.connect(lambda _results: self.show_following_homepage_prompts())
        self.browse_page.set_favorite_handlers(
            is_favorited=lambda item: self._favorites_controller.is_favorited(
                source_kind="browse",
                source_key="",
                vod_id=item.vod_id,
            ),
            toggle_favorite=self._toggle_browse_favorite,
        )
        self.douban_page.search_requested.connect(self._handle_douban_search_requested)
        self._connect_video_item_context_menu(self.douban_page)
        self.global_catalog_page.search_requested.connect(self._handle_douban_search_requested)
        self.global_catalog_page.item_open_requested.connect(self.open_media_detail_from_vod)
        self._connect_video_item_context_menu(self.global_catalog_page)
        self._connect_video_item_context_menu(self.telegram_page)
        self.telegram_page.item_open_requested.connect(self._handle_telegram_item_open_requested)
        self.telegram_page.open_requested.connect(self._handle_telegram_open_requested)
        if self.telegram_channel_page is not None:
            telegram_channel_page = self.telegram_channel_page
            self._connect_video_item_context_menu(telegram_channel_page)
            telegram_channel_page.item_open_requested.connect(self._handle_telegram_channel_item_open_requested)
            telegram_channel_page.folder_breadcrumb_requested.connect(
                lambda node_id, kind, index, page=telegram_channel_page: self._handle_media_breadcrumb_requested(
                    page,
                    self.telegram_channel_controller,
                    node_id,
                    kind,
                    index,
                )
            )
        if self.bilibili_page is not None:
            bilibili_page = self.bilibili_page
            self._connect_video_item_context_menu(bilibili_page)
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
        if self.youtube_page is not None:
            self._connect_video_item_context_menu(self.youtube_page)
            self.youtube_page.item_open_requested.connect(self._handle_youtube_item_open_requested)
        self._connect_video_item_context_menu(self.live_page)
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
            self._connect_video_item_context_menu(emby_page)
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
            self._connect_video_item_context_menu(jellyfin_page)
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
            self._connect_video_item_context_menu(feiniu_page)
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
            self._connect_video_item_context_menu(self.pansou_page)
            self.pansou_page.item_open_requested.connect(self._handle_pansou_item_open_requested)

        self.douban_page.unauthorized.connect(self.logout_requested.emit)
        self.douban_page.selected_category_changed.connect(
            lambda category_id, page=self.douban_page: self._handle_selected_category_changed(page, category_id)
        )
        self.global_catalog_page.unauthorized.connect(self.logout_requested.emit)
        self.global_catalog_page.selected_category_changed.connect(
            lambda category_id, page=self.global_catalog_page: self._handle_selected_category_changed(page, category_id)
        )
        self.telegram_page.unauthorized.connect(self.logout_requested.emit)
        self.telegram_page.selected_category_changed.connect(
            lambda category_id, page=self.telegram_page: self._handle_selected_category_changed(page, category_id)
        )
        if self.telegram_channel_page is not None:
            self.telegram_channel_page.unauthorized.connect(self.logout_requested.emit)
            self.telegram_channel_page.selected_category_changed.connect(
                lambda category_id, page=self.telegram_channel_page: self._handle_selected_category_changed(page, category_id)
            )
        if self.bilibili_page is not None:
            self.bilibili_page.unauthorized.connect(self.logout_requested.emit)
            self.bilibili_page.selected_category_changed.connect(
                lambda category_id, page=self.bilibili_page: self._handle_selected_category_changed(page, category_id)
            )
        if self.youtube_page is not None:
            self.youtube_page.unauthorized.connect(self.logout_requested.emit)
            self.youtube_page.selected_category_changed.connect(
                lambda category_id, page=self.youtube_page: self._handle_selected_category_changed(page, category_id)
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
        self.apply_home_mode(getattr(self.config, "home_mode", "browse") or "browse")
        self._sync_startup_plugin_loading_ui()
        self._sync_global_search_action_state()
        self._apply_theme()
        self._handle_tab_changed(self.nav_tabs.currentIndex())

    def _apply_global_search_button_theme(self) -> None:
        tokens = current_tokens()
        is_dark = current_resolved_theme() == "dark"
        button_qss = build_round_icon_button_qss(
            tokens,
            background=tokens.button_bg if is_dark else None,
            border_color=tokens.input_hover_border if is_dark else None,
            text_color=tokens.text_primary,
            hover_background=tokens.panel_alt_bg if is_dark else None,
            hover_border_color=tokens.accent_hover,
        )
        self.global_search_button.setStyleSheet(button_qss)
        self.global_search_popup_button.setStyleSheet(button_qss)
        self.heat_recommendations_button.setStyleSheet(button_qss)
        self.global_search_button.setIcon(load_tinted_icon(self._SEARCH_ICON_PATH, tokens.text_primary))
        self.global_search_popup_button.setIcon(load_tinted_icon(self._SEARCH_POPUP_ICON_PATH, tokens.text_primary))
        self.heat_recommendations_button.setIcon(
            load_tinted_icon(self._HEAT_RECOMMENDATIONS_ICON_PATH, tokens.text_primary)
        )

    def _configure_header_icon_button(self, button: QPushButton, tooltip: str) -> None:
        button.setText("")
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setFixedSize(36, 36)
        button.setIconSize(QSize(20, 20))
        button.setCursor(Qt.CursorShape.PointingHandCursor)

    def _create_header_action_separator(self) -> QFrame:
        separator = QFrame(self)
        separator.setObjectName("headerActionSeparator")
        separator.setFixedSize(1, 22)
        separator.setFrameShape(QFrame.Shape.NoFrame)
        return separator

    def _apply_header_action_button_theme(self) -> None:
        tokens = current_tokens()
        button_qss = build_round_icon_button_qss(tokens)
        button_icons = {
            self.home_button: self._HOME_ICON_PATH,
            self.browse_button: self._BROWSE_ICON_PATH,
            self.favorites_button: self._FAVORITES_ICON_PATH,
            self.following_button: self._FOLLOWING_ICON_PATH,
            self.history_button: self._HISTORY_ICON_PATH,
            self.plugin_manager_button: self._PLUGIN_MANAGER_ICON_PATH,
            self.live_source_manager_button: self._LIVE_SOURCE_MANAGER_ICON_PATH,
            self.advanced_settings_button: self._ADVANCED_SETTINGS_ICON_PATH,
            self.logout_button: self._LOGOUT_ICON_PATH,
        }
        for button, icon_path in button_icons.items():
            button.setStyleSheet(button_qss)
            button.setIcon(load_tinted_icon(icon_path, tokens.text_primary, size=button.iconSize()))
        for separator in getattr(self, "header_action_separators", []):
            separator.setStyleSheet(f"QFrame#headerActionSeparator {{ background: {tokens.border_subtle}; }}")

    def _apply_navigation_tab_theme(self) -> None:
        tokens = current_tokens()
        self.nav_tabs.tab_bar.setStyleSheet(build_navigation_tabbar_qss(tokens))
        self.plugin_overflow_button.setStyleSheet(
            build_pill_button_qss(tokens, checked_accent=True, border_radius=12, horizontal_padding=8)
        )

    def _apply_theme(self) -> None:
        tokens = current_tokens()
        self.global_search_edit.setStyleSheet(build_search_line_edit_qss(tokens, border_radius=18, min_height=36))
        self._apply_global_search_button_theme()
        self._apply_header_action_button_theme()
        self._apply_navigation_tab_theme()
        if self._global_search_popup is not None:
            self._global_search_popup._apply_theme()
        if self._heat_recommendations_dialog is not None:
            self._heat_recommendations_dialog._apply_theme()
        for page in (
            self.browse_page,
            self.history_page,
            self.global_history_page,
            self.douban_page,
            self.global_catalog_page,
            self.telegram_page,
            self.live_page,
            self.emby_page,
            self.telegram_channel_page,
            self.bilibili_page,
            self.youtube_page,
            self.jellyfin_page,
            self.feiniu_page,
            self.pansou_page,
        ):
            apply_theme = getattr(page, "_apply_theme", None)
            if callable(apply_theme):
                apply_theme()
        for page, _controller, _plugin_id in self._plugin_pages:
            apply_theme = getattr(page, "_apply_theme", None)
            if callable(apply_theme):
                apply_theme()
        if hasattr(self, "_classic_home_page"):
            self._classic_home_page._apply_theme()
        if hasattr(self, "_simplified_home_page"):
            self._simplified_home_page._apply_theme()
        if hasattr(self, "_media_home_page"):
            self._media_home_page._apply_theme()

    _HOME_MODE_LABELS = {
        "classic": "经典模式 (TvBox)",
        "simplified": "精简模式 (搜索)",
        "media": "媒体模式 (Emby)",
        "tv": "电视模式 (直播)",
    }

    def apply_home_mode(self, mode: str) -> None:
        normalized = mode if mode in {"browse", "classic", "simplified", "media", "tv"} else "browse"
        self._active_home_mode = normalized
        if normalized == "browse":
            self._hide_classic_header_source_picker()
            self.nav_tabs.setNavigationVisible(True)
            self._home_stack.setCurrentWidget(self.nav_tabs)
            self.nav_tabs.setVisible(True)
            self.global_search_container.setVisible(True)
            self.home_button.setVisible(False)
            self._refresh_navigation_tabs()
            self._start_deferred_startup_plugin_load_if_needed()
            return
        if normalized == "classic":
            self._apply_classic_home_mode()
            return
        if normalized == "simplified":
            self._apply_simplified_home_mode()
            return
        if normalized == "media":
            self._apply_media_home_mode()
            return
        # Placeholder for unimplemented modes
        if not hasattr(self, "_home_mode_placeholder"):
            from PySide6.QtWidgets import QVBoxLayout
            self._home_mode_placeholder = QWidget()
            layout = QVBoxLayout(self._home_mode_placeholder)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._home_mode_placeholder_label = QLabel()
            self._home_mode_placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._home_mode_placeholder_label.setStyleSheet("font-size: 18px; font-weight: 600;")
            layout.addWidget(self._home_mode_placeholder_label)
            self._home_stack.addWidget(self._home_mode_placeholder)
        label = self._HOME_MODE_LABELS.get(normalized, normalized)
        self._home_mode_placeholder_label.setText(f"{label}\n\n开发中…")
        self._home_stack.setCurrentWidget(self._home_mode_placeholder)
        self.nav_tabs.setVisible(False)
        self.nav_tabs.setNavigationVisible(True)
        self.global_search_container.setVisible(normalized in {"classic", "simplified"})
        self.home_button.setVisible(True)
        self._hide_classic_header_source_picker()

    def _apply_media_home_mode(self) -> None:
        page = self._ensure_media_home_page()
        self._hide_classic_header_source_picker()
        self._home_stack.setCurrentWidget(page)
        self.nav_tabs.setNavigationVisible(True)
        self.nav_tabs.setVisible(False)
        self.global_search_container.setVisible(True)
        self.home_button.setVisible(True)
        self._start_deferred_startup_plugin_load_if_needed()
        page.refresh_content()

    def _ensure_media_home_page(self) -> MediaHomePage:
        if not hasattr(self, "_media_home_page"):
            self._media_home_page = MediaHomePage(self._load_media_home_sections)
            self._media_home_page.current_play_requested.connect(self.show_or_restore_player)
            self._media_home_page.continue_requested.connect(self.open_history_detail)
            self._media_home_page.following_requested.connect(self.open_following_detail)
            self._media_home_page.favorite_requested.connect(self.open_favorite_detail)
            self._home_stack.addWidget(self._media_home_page)
        return self._media_home_page

    def _refresh_media_home_if_active(self) -> None:
        if not hasattr(self, "_media_home_page"):
            return
        if self._home_stack.currentWidget() is not self._media_home_page:
            return
        self._media_home_page.refresh_content()

    def _load_media_home_sections(self) -> MediaHomeSections:
        return MediaHomeSections(
            current_playing=self._current_playing_media_home_card(),
            continue_watching=self._continue_watching_media_home_cards(),
            following=self._following_media_home_cards(),
            favorites=self._favorite_media_home_cards(),
        )

    def _current_playing_media_home_card(self) -> MediaHomeCard | None:
        if self.player_window is None or getattr(self.player_window, "session", None) is None:
            return self._restorable_current_media_home_card()
        session = self.player_window.session
        vod = session.vod
        playlist = list(getattr(session, "playlist", []) or [])
        current_index = max(0, int(getattr(self.player_window, "current_index", 0) or 0))
        current_item = playlist[current_index] if 0 <= current_index < len(playlist) else None
        title_candidates = [
            str(getattr(vod, "vod_name", "") or "").strip(),
            str(getattr(session, "initial_vod_name", "") or "").strip(),
        ]
        if current_item is not None:
            title_candidates.extend(
                [
                    str(getattr(current_item, "media_title", "") or "").strip(),
                    str(getattr(current_item, "title", "") or "").strip(),
                ]
            )
        title_candidates.append(str(getattr(vod, "vod_id", "") or "").strip())
        title = next((candidate for candidate in title_candidates if candidate), "")
        if not title:
            title = "当前播放"
        subtitle_parts = ["正在播放"]
        if current_item is not None and str(current_item.title or "").strip():
            subtitle_parts.append(str(current_item.title or "").strip())
        if len(playlist) > 1:
            subtitle_parts.append(f"{current_index + 1}/{len(playlist)}")
        poster = str(getattr(vod, "vod_pic", "") or "").strip()
        if current_item is not None:
            poster = poster or str(getattr(current_item, "video_cover_override", "") or "").strip()
        return MediaHomeCard(
            title=title,
            subtitle=" · ".join(subtitle_parts),
            poster=poster,
        )

    def _restorable_current_media_home_card(self) -> MediaHomeCard | None:
        playback_id = str(getattr(self.config, "last_playback_vod_id", "") or "").strip()
        clicked_id = str(getattr(self.config, "last_playback_clicked_vod_id", "") or "").strip()
        playback_path = str(getattr(self.config, "last_playback_path", "") or "").strip()
        if not (playback_id or clicked_id or playback_path):
            return None
        record = self._find_restorable_history_record(
            playback_id=playback_id,
            clicked_id=clicked_id,
        )
        if record is not None:
            card = self._history_media_home_card(record)
            return MediaHomeCard(
                title=card.title,
                subtitle=card.subtitle,
                poster=card.poster,
                payload=card.payload,
            )
        source = str(getattr(self.config, "last_playback_source", "") or "browse")
        source_key = str(getattr(self.config, "last_playback_source_key", "") or "")
        title = playback_id or clicked_id or playback_path or "恢复播放"
        return MediaHomeCard(
            title=title,
            subtitle=self._favorite_source_name(source, source_key),
        )

    def _find_restorable_history_record(
        self,
        *,
        playback_id: str,
        clicked_id: str,
    ) -> HistoryRecord | None:
        load_page = getattr(self._history_controller, "load_page", None)
        if not callable(load_page):
            return None
        try:
            records, _total = load_page(page=1, size=100, keyword="")
        except TypeError:
            return None
        except Exception:
            return None
        source = str(getattr(self.config, "last_playback_source", "") or "").strip()
        source_key = str(getattr(self.config, "last_playback_source_key", "") or "").strip()
        candidates = {value for value in (playback_id, clicked_id) if value}
        for record in records:
            key = str(getattr(record, "key", "") or "").strip()
            if key not in candidates:
                continue
            record_source = str(getattr(record, "source_kind", "") or "").strip()
            if source == "plugin":
                if record_source not in {"plugin", "spider_plugin"}:
                    continue
                record_key = str(getattr(record, "source_key", "") or "").strip()
                record_plugin_id = str(getattr(record, "source_plugin_id", "") or "").strip()
                if source_key and source_key not in {record_key, record_plugin_id}:
                    continue
            elif source and record_source and record_source != source:
                continue
            return record
        return None

    def _continue_watching_media_home_cards(self) -> list[MediaHomeCard]:
        load_page = getattr(self._history_controller, "load_page", None)
        if not callable(load_page):
            return []
        try:
            records, _total = load_page(
                page=1,
                size=12,
                keyword="",
                continue_watching=True,
            )
        except TypeError:
            try:
                records, _total = load_page(page=1, size=12, keyword="")
            except Exception:
                return []
            records = [record for record in records if int(getattr(record, "position", 0) or 0) > 0]
        except Exception:
            return []
        return [self._history_media_home_card(record) for record in list(records)[:12]]

    def _history_media_home_card(self, record: HistoryRecord) -> MediaHomeCard:
        episode = int(getattr(record, "episode", 0) or 0)
        remark = str(getattr(record, "vod_remarks", "") or "").strip()
        subtitle = remark
        if not subtitle and episode >= 0:
            subtitle = f"第 {episode + 1} 集"
        return MediaHomeCard(
            title=str(getattr(record, "vod_name", "") or getattr(record, "key", "") or "播放记录"),
            subtitle=subtitle,
            poster=str(getattr(record, "vod_pic", "") or ""),
            payload=record,
        )

    def _following_media_home_cards(self) -> list[MediaHomeCard]:
        load_page = getattr(self._following_controller, "load_page", None)
        if not callable(load_page):
            return []
        try:
            items, _total = load_page(page=1, size=12, keyword="", only_updates=False)
        except Exception:
            return []
        cards: list[MediaHomeCard] = []
        for item in list(items)[:12]:
            record = getattr(item, "record", item)
            title = str(getattr(item, "display_title", "") or getattr(record, "title", "") or "追剧")
            subtitle = str(
                getattr(item, "update_text", "")
                or getattr(item, "progress_text", "")
                or getattr(item, "subtitle", "")
                or ""
            ).strip()
            cards.append(
                MediaHomeCard(
                    title=title,
                    subtitle=subtitle,
                    poster=str(getattr(record, "poster", "") or ""),
                    payload=int(getattr(record, "id", 0) or 0),
                )
            )
        return cards

    def _favorite_media_home_cards(self) -> list[MediaHomeCard]:
        load_page = getattr(self._favorites_controller, "load_page", None)
        if not callable(load_page):
            return []
        try:
            items, _total = load_page(page=1, size=12, keyword="")
        except Exception:
            return []
        cards: list[MediaHomeCard] = []
        for item in list(items)[:12]:
            record = getattr(item, "record", item)
            title = str(
                getattr(item, "display_title", "")
                or getattr(record, "latest_vod_name", "")
                or getattr(record, "vod_name_snapshot", "")
                or "收藏"
            )
            source_label = self._favorite_record_source_label(record)
            remark = str(getattr(record, "vod_remarks", "") or "").strip()
            cards.append(
                MediaHomeCard(
                    title=title,
                    subtitle=" · ".join(part for part in (source_label, remark) if part),
                    poster=str(getattr(record, "vod_pic", "") or ""),
                    payload=record,
                )
            )
        return cards

    def _apply_simplified_home_mode(self) -> None:
        page = self._ensure_simplified_home_page()
        self._hide_classic_header_source_picker()
        self._home_stack.setCurrentWidget(page)
        self.nav_tabs.setNavigationVisible(True)
        self.nav_tabs.setVisible(False)
        self.global_search_container.setVisible(False)
        self.home_button.setVisible(True)
        self._start_deferred_startup_plugin_load_if_needed()
        page.refresh_content()

    def _ensure_simplified_home_page(self) -> SimplifiedHomePage:
        if not hasattr(self, "_simplified_home_page"):
            self._simplified_home_page = SimplifiedHomePage(
                hotword_loader=self._load_simplified_hotwords,
                recommendation_loader=self._load_simplified_recommendations,
            )
            self._simplified_home_page.search_requested.connect(self._handle_simplified_search_requested)
            self._home_stack.addWidget(self._simplified_home_page)
        return self._simplified_home_page

    def _handle_simplified_search_requested(self, keyword: str) -> None:
        normalized_keyword = str(keyword or "").strip()
        if not normalized_keyword:
            return
        self.global_search_edit.setText(normalized_keyword)
        self.global_search_container.setVisible(True)
        self.nav_tabs.setNavigationVisible(True)
        self.nav_tabs.setVisible(True)
        self._home_stack.setCurrentWidget(self.nav_tabs)
        self._start_deferred_startup_plugin_load_if_needed()
        self._start_global_search()

    def _load_simplified_hotwords(self) -> list[dict[str, str]]:
        source = self._global_search_hotkey_active_source
        hot_type = self._fallback_global_search_hot_category(
            source,
            self._global_search_hotkey_preferred_type,
        )
        payload = self._call_global_search_hotkey_loader(source, hot_type)
        result = self._normalize_global_search_hotkey_load_result(source, hot_type, payload)
        return [
            {
                "title": str(item.get("title") or "").strip(),
                "query": str(item.get("query") or item.get("title") or "").strip(),
            }
            for item in result.items
            if str(item.get("title") or "").strip()
        ]

    def _load_simplified_recommendations(self) -> list[VodItem]:
        controller = getattr(self.douban_page, "controller", None)
        if controller is None or not callable(getattr(controller, "load_items", None)):
            return []
        category_id = self._simplified_recommendation_category_id(controller)
        if not category_id:
            return []
        items, _total = self._load_controller_items(controller, category_id, 1)
        return list(items)

    def _simplified_recommendation_category_id(self, controller: Any) -> str:
        load_categories = getattr(controller, "load_categories", None)
        if not callable(load_categories):
            return ""
        try:
            categories = list(load_categories())
        except Exception:
            return ""
        if not categories:
            return ""
        preferred_terms = ("热门推荐", "热门", "推荐", "热映", "电影")
        for term in preferred_terms:
            for category in categories:
                category_id = str(getattr(category, "type_id", "") or "").strip()
                category_title = str(getattr(category, "type_name", "") or "").strip()
                if category_id and term in f"{category_id} {category_title}":
                    return category_id
        return str(getattr(categories[0], "type_id", "") or "").strip()

    @staticmethod
    def _load_controller_items(controller: Any, category_id: str, page: int) -> tuple[list[Any], int]:
        try:
            return controller.load_items(category_id, page, filters=None)
        except TypeError:
            return controller.load_items(category_id, page)

    def _start_deferred_startup_plugin_load_if_needed(self) -> None:
        if self._startup_plugin_load_started or not callable(self._plugin_loader_task):
            return
        self._startup_plugin_load_state = "loading"
        self._startup_plugin_load_error = ""
        self._sync_startup_plugin_loading_ui()
        self._start_startup_plugin_load()

    def _build_classic_source_entries(self) -> list[SourceEntry]:
        builtin_entries: list[SourceEntry] = []
        plugin_entries: list[SourceEntry] = []
        for definition in self._visible_builtin_tab_definitions():
            if definition.global_search_only:
                continue
            page = definition.page
            controller = definition.search_controller
            if controller is None and isinstance(page, PosterGridPage):
                controller = getattr(page, "controller", None)
            builtin_entries.append(
                SourceEntry(
                    key=definition.key,
                    title=definition.title,
                    controller=controller,
                    search_enabled=controller is not None and callable(getattr(controller, "search_items", None)),
                    source_kind="builtin",
                )
            )
        loaded_plugin_entries: dict[str, SourceEntry] = {}
        for definition in self._plugin_tab_definitions:
            controller = definition.search_controller
            if controller is not None:
                loaded_plugin_entries[definition.key] = SourceEntry(
                    key=definition.key,
                    title=definition.title,
                    controller=controller,
                    search_enabled=callable(getattr(controller, "search_items", None)),
                    source_kind="plugin",
                )
        plugin_metadata_entries = self._classic_plugin_metadata_entries()
        if plugin_metadata_entries:
            for entry in plugin_metadata_entries:
                plugin_entries.append(loaded_plugin_entries.pop(entry.key, entry))
            plugin_entries.extend(loaded_plugin_entries.values())
        else:
            plugin_entries.extend(loaded_plugin_entries.values())
        return [*builtin_entries, *plugin_entries]

    def _classic_plugin_metadata_entries(self) -> list[SourceEntry]:
        if self._plugin_manager is None:
            return []
        list_plugins = getattr(self._plugin_manager, "list_plugins", None)
        if not callable(list_plugins):
            return []
        entries: list[SourceEntry] = []
        try:
            plugins = list_plugins()
        except Exception:
            return []
        for plugin in plugins or []:
            if not bool(getattr(plugin, "enabled", False)):
                continue
            plugin_id = str(getattr(plugin, "id", "") or "")
            if not plugin_id:
                continue
            title = str(getattr(plugin, "display_name", "") or getattr(plugin, "name", "") or "插件")
            entries.append(
                SourceEntry(
                    key=f"plugin:{plugin_id}",
                    title=title,
                    controller=None,
                    search_enabled=True,
                    source_kind="plugin",
                )
            )
        return entries

    def _apply_classic_home_mode(self) -> None:
        entries = self._build_classic_source_entries()
        if not entries:
            return
        initial_key = ""
        saved_tab = getattr(self.config, "last_selected_tab", "") or ""
        if saved_tab:
            matching = [e for e in entries if e.key == saved_tab]
            if matching:
                initial_key = saved_tab
        if not initial_key:
            initial_key = entries[0].key
        if not hasattr(self, "_classic_home_page"):
            self._classic_home_page = ClassicHomePage(
                entries,
                initial_source_key=initial_key,
                initial_category_id=self._initial_category_id_for_tab(initial_key),
                filter_panel_state=self._filter_panel_state,
            )
            self._classic_home_page.item_open_requested.connect(self._handle_classic_item_open)
            self._classic_home_page.source_changed.connect(self._handle_classic_source_changed)
            self._classic_home_page.category_selected.connect(self._handle_classic_category_selected)
            self._classic_home_page.folder_breadcrumb_requested.connect(self._handle_classic_folder_breadcrumb_requested)
            self._home_stack.addWidget(self._classic_home_page)
        else:
            self._classic_home_page.set_source_entries(entries, preferred_key=initial_key)
        self._show_classic_header_source_picker()
        self._home_stack.setCurrentWidget(self._classic_home_page)
        self.nav_tabs.setNavigationVisible(False)
        self.nav_tabs.setVisible(False)
        self.global_search_container.setVisible(True)
        self.home_button.setVisible(False)
        self._handle_classic_source_changed(self._classic_home_page.current_source_key())

    def _refresh_classic_source_entries_if_active(self) -> None:
        if not hasattr(self, "_classic_home_page"):
            return
        preferred_key = str(getattr(self.config, "last_selected_tab", "") or "")
        self._classic_home_page.set_source_entries(self._build_classic_source_entries(), preferred_key=preferred_key)

    def _handle_classic_source_changed(self, source_key: str) -> None:
        if source_key and self.config.last_selected_tab != source_key:
            self.config.last_selected_tab = source_key
            self._save_config()
        if source_key and not source_key.startswith("plugin:"):
            definition = self._builtin_tab_definition_by_key(source_key)
            if definition is not None and not isinstance(definition.page, PosterGridPage):
                self._show_builtin_page_in_home_stack(definition)
            elif hasattr(self, "_classic_home_page"):
                self._home_stack.setCurrentWidget(self._classic_home_page)
                self.nav_tabs.setNavigationVisible(False)
                self.nav_tabs.setVisible(False)
        if not source_key.startswith("plugin:"):
            return
        if hasattr(self, "_classic_home_page"):
            self._home_stack.setCurrentWidget(self._classic_home_page)
            self.nav_tabs.setNavigationVisible(False)
            self.nav_tabs.setVisible(False)
        if next((entry for entry in self._build_classic_source_entries() if entry.key == source_key and entry.controller is not None), None):
            return
        plugin_id = source_key.removeprefix("plugin:")
        self._ensure_classic_plugin_sources_loaded([plugin_id])

    def _ensure_classic_global_search_plugins_loaded(self) -> None:
        missing_plugin_ids = [
            entry.key.removeprefix("plugin:")
            for entry in self._build_classic_source_entries()
            if entry.key.startswith("plugin:") and entry.controller is None
        ]
        self._ensure_classic_plugin_sources_loaded(missing_plugin_ids)

    def _ensure_classic_plugin_sources_loaded(self, plugin_ids: Iterable[str]) -> bool:
        requested_ids: list[str] = []
        loaded_plugin_ids = {
            definition.key.removeprefix("plugin:")
            for definition in self._plugin_tab_definitions
            if definition.key.startswith("plugin:")
        }
        for plugin_id in plugin_ids:
            normalized_id = str(plugin_id or "").strip().removeprefix("plugin:")
            if not normalized_id or normalized_id in loaded_plugin_ids or normalized_id in requested_ids:
                continue
            requested_ids.append(normalized_id)
        if not requested_ids:
            return False
        loaded_definitions = self._load_plugin_definitions_with_manager("load_plugins", requested_ids)
        if not loaded_definitions:
            return False
        loaded_by_id = {str(_plugin_value(definition, "id") or ""): definition for definition in loaded_definitions}
        changed = False
        for plugin_id in requested_ids:
            definition = loaded_by_id.get(plugin_id)
            if definition is None:
                continue
            existing_index = next(
                (
                    index
                    for index, current in enumerate(self._plugin_definitions)
                    if str(_plugin_value(current, "id") or "") == plugin_id
                ),
                -1,
            )
            if existing_index >= 0:
                self._plugin_definitions[existing_index] = definition
            else:
                self._plugin_definitions.append(definition)
            page_entry = next((entry for entry in self._plugin_pages if entry[2] == plugin_id), None)
            if page_entry is not None:
                page_entry[0].deleteLater()
                self._plugin_pages = [entry for entry in self._plugin_pages if entry[2] != plugin_id]
                self._plugin_tab_definitions = [
                    current for current in self._plugin_tab_definitions if current.key != f"plugin:{plugin_id}"
                ]
            page, controller, current_plugin_id, tab_definition = self._create_plugin_page_entry(definition)
            self._plugin_pages.append((page, controller, current_plugin_id))
            self._plugin_tab_definitions.append(tab_definition)
            changed = True
        if changed:
            self._refresh_classic_source_entries_if_active()
            self._refresh_visible_tabs()
        return changed

    def _handle_classic_category_selected(self, category_id: str) -> None:
        if not hasattr(self, "_classic_home_page"):
            return
        self._remember_selected_category_key(self._classic_home_page.current_source_key(), category_id)

    def _show_classic_header_source_picker(self) -> None:
        if not hasattr(self, "_classic_home_page"):
            return
        source_button = self._classic_home_page.source_button
        if self.header_layout.indexOf(source_button) < 0:
            self.header_layout.insertWidget(0, source_button)
        source_button.show()
        self.header_layout.invalidate()

    def _hide_classic_header_source_picker(self) -> None:
        if hasattr(self, "_classic_home_page"):
            source_button = self._classic_home_page.source_button
            if self.header_layout.indexOf(source_button) >= 0:
                self.header_layout.removeWidget(source_button)
                source_button.setParent(self._classic_home_page)
                source_button.hide()
            self._classic_home_page._hide_source_popup()
        self.header_layout.invalidate()

    def _handle_classic_item_open(self, item) -> None:
        if not hasattr(self, "_classic_home_page"):
            return
        source_key = self._classic_home_page.current_source_key()
        entry = next(
            (e for e in self._build_classic_source_entries() if e.key == source_key),
            None,
        )
        if entry is None:
            return
        controller = entry.controller
        if source_key.startswith("plugin:"):
            plugin_id = source_key.replace("plugin:", "", 1)
            self._open_spider_item(controller, plugin_id, item)
            return
        self._handle_classic_builtin_item_open(source_key, controller, item)

    def _handle_classic_folder_breadcrumb_requested(self, node_id: str, kind: str, index: int) -> None:
        if not hasattr(self, "_classic_home_page"):
            return
        source_key = self._classic_home_page.current_source_key()
        entry = next(
            (e for e in self._build_classic_source_entries() if e.key == source_key),
            None,
        )
        if entry is None or entry.controller is None:
            return
        self._handle_media_breadcrumb_requested(
            self._classic_home_page.grid_page,
            entry.controller,
            node_id,
            kind,
            index,
        )

    def _handle_classic_builtin_item_open(self, source_key: str, controller: Any, item: Any) -> None:
        if source_key == "douban":
            self._handle_douban_search_requested(str(getattr(item, "vod_name", "") or ""))
            return
        if source_key == "telegram":
            self._skip_next_telegram_open_request_vod_id = str(getattr(item, "vod_id", "") or "")

            def build_request() -> OpenPlayerRequest:
                request = controller.build_request(getattr(item, "vod_id", ""))
                return self._apply_request_fallback_metadata(request, item, prefer_fallback_media_title=True)

            self._start_open_request(build_request)
            return
        if source_key in {"bilibili", "live", "emby", "jellyfin", "feiniu"} and getattr(item, "vod_tag", "") == "folder":
            if hasattr(self, "_classic_home_page"):
                self._open_media_folder(self._classic_home_page.grid_page, controller, item)
            return
        if source_key == "youtube":
            self._start_youtube_item_open(controller, item)
            return
        if callable(getattr(controller, "build_request", None)):
            self._start_open_request(lambda: controller.build_request(getattr(item, "vod_id", "")))

    def show_browse_path(self, path: str) -> None:
        self.browse_page.load_path(path)
        self.nav_tabs.setNavigationVisible(True)
        self.nav_tabs.setVisible(True)
        self._home_stack.setCurrentWidget(self.nav_tabs)
        self.nav_tabs.setCurrentWidget(self.browse_page)

    def _all_tab_definitions(self) -> list[_TabDefinition]:
        builtin_definitions = [
            _TabDefinition(
                definition.key,
                definition.title,
                definition.page,
                definition.search_controller,
                definition.global_search_only,
            )
            for definition in self._builtin_tab_definitions
        ]
        global_search_only_definitions = [
            definition
            for definition in self._static_tab_definitions
            if definition.global_search_only
            and all(definition.key != builtin.key for builtin in builtin_definitions)
        ]
        return [*builtin_definitions, *global_search_only_definitions, *self._plugin_tab_definitions]

    def _builtin_tab_default_definitions(self) -> list[_BuiltinTabDefinition]:
        definitions: list[_BuiltinTabDefinition] = [
            _BuiltinTabDefinition("douban", "豆瓣电影", self.douban_page),
            _BuiltinTabDefinition("global_catalog", "全球片单", self.global_catalog_page),
            _BuiltinTabDefinition("telegram", "电报影视", self.telegram_page, self.telegram_controller),
        ]
        if self.telegram_channel_page is not None:
            definitions.append(_BuiltinTabDefinition("telegram_channel", "电报频道", self.telegram_channel_page, self.telegram_channel_controller))
        if self.bilibili_page is not None:
            definitions.append(_BuiltinTabDefinition("bilibili", "B站", self.bilibili_page, self.bilibili_controller))
        if self.youtube_page is not None:
            definitions.append(_BuiltinTabDefinition("youtube", "YouTube", self.youtube_page, self.youtube_controller))
        definitions.append(_BuiltinTabDefinition("live", "网络直播", self.live_page))
        if self.emby_page is not None:
            definitions.append(_BuiltinTabDefinition("emby", "Emby", self.emby_page, self.emby_controller))
        if self.jellyfin_page is not None:
            definitions.append(_BuiltinTabDefinition("jellyfin", "Jellyfin", self.jellyfin_page, self.jellyfin_controller))
        if self.feiniu_page is not None:
            definitions.append(_BuiltinTabDefinition("feiniu", "飞牛影视", self.feiniu_page, self.feiniu_controller))
        definitions.extend(
            [
                _BuiltinTabDefinition("browse", "文件浏览", self.browse_page, trailing=True),
                _BuiltinTabDefinition("favorites", "我的收藏", self.favorites_page, self._favorites_controller, trailing=True),
                _BuiltinTabDefinition("following", "我的追更", self.following_page, self._following_controller, trailing=True),
                _BuiltinTabDefinition("history", "播放记录", self.history_page, trailing=True),
            ]
        )
        return definitions

    def _build_builtin_tab_definitions(self) -> list[_BuiltinTabDefinition]:
        default_definitions = self._builtin_tab_default_definitions()
        overrides = parse_builtin_tab_overrides_json(getattr(self.config, "builtin_tab_overrides_json", ""))
        by_key = {definition.key: definition for definition in default_definitions}
        ordered_keys: list[str] = []
        for key in overrides.order:
            if key in by_key and key not in ordered_keys:
                ordered_keys.append(key)
        for definition in default_definitions:
            if definition.key not in ordered_keys:
                ordered_keys.append(definition.key)
        hidden = set(overrides.hidden)
        result: list[_BuiltinTabDefinition] = []
        for key in ordered_keys:
            definition = by_key[key]
            title = overrides.renames.get(key, definition.title)
            result.append(
                _BuiltinTabDefinition(
                    key=definition.key,
                    title=title,
                    page=definition.page,
                    search_controller=definition.search_controller,
                    global_search_only=definition.global_search_only,
                    trailing=definition.trailing,
                )
            )
        self._builtin_hidden_keys = hidden
        return result

    def _builtin_tab_definition_by_key(self, key: str) -> _BuiltinTabDefinition | None:
        for definition in self._builtin_tab_definitions:
            if definition.key == key:
                return definition
        return None

    def _visible_builtin_tab_definitions(self) -> list[_BuiltinTabDefinition]:
        hidden_keys = getattr(self, "_builtin_hidden_keys", set())
        return [definition for definition in self._builtin_tab_definitions if definition.key not in hidden_keys]

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
            key: f"{result.title}({result.total_count})"
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
        button_spacing = 8
        tab_bar_width = self.nav_tabs.tab_bar.width()
        total_nav_width = tab_bar_width
        if self.plugin_overflow_button.isVisible():
            total_nav_width += self._plugin_overflow_button_width() + button_spacing
        if total_nav_width <= 0:
            total_nav_width = max(self.nav_tabs.width(), 0)
        builtin_width = sum(
            self._plugin_tab_title_width(definition.title)
            for definition in self._visible_builtin_tab_definitions()
            if not definition.global_search_only
        )
        available = total_nav_width - builtin_width
        total_plugin_width = sum(
            self._plugin_tab_title_width(definition.title)
            for definition in self._plugin_tab_definitions
        )
        if self._plugin_tab_definitions and total_plugin_width > available:
            available -= self._plugin_overflow_button_width() + button_spacing
        return max(available, 0)

    def _split_visible_and_hidden_plugin_tabs(self) -> tuple[list[_TabDefinition], list[_TabDefinition]]:
        if self._global_search_restore_plugin_keys:
            restore_keys = set(self._global_search_restore_plugin_keys)
            visible = [definition for definition in self._plugin_tab_definitions if definition.key in restore_keys]
            hidden = [definition for definition in self._plugin_tab_definitions if definition.key not in restore_keys]
            return visible, hidden
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

    def _split_visible_and_hidden_search_tabs(
        self,
        definitions: list[_TabDefinition],
        title_overrides: dict[str, str],
    ) -> tuple[list[_TabDefinition], list[_TabDefinition]]:
        button_spacing = 8
        total_nav_width = self.nav_tabs.tab_bar.width()
        if total_nav_width <= 0:
            total_nav_width = max(self.nav_tabs.width(), 0)
        widths = [
            (definition, self._plugin_tab_title_width(title_overrides.get(definition.key, definition.title)))
            for definition in definitions
        ]
        total_width = sum(width for _definition, width in widths)
        available = total_nav_width
        if definitions and total_width > available:
            available -= self._plugin_overflow_button_width() + button_spacing
        visible: list[_TabDefinition] = []
        hidden: list[_TabDefinition] = []
        used = 0
        for definition, width in widths:
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
            result_definitions = [
                definition for definition in self._all_tab_definitions() if definition.key in self._global_search_results
            ]
            definitions, hidden_results = self._split_visible_and_hidden_search_tabs(result_definitions, title_overrides)
            self._hidden_plugin_tab_definitions = hidden_results
            self.plugin_overflow_button.setVisible(bool(hidden_results))
            self.plugin_overflow_button.setText(f"更多({len(hidden_results)})" if hidden_results else "更多")
            if not hidden_results:
                self._close_plugin_overflow_drawer()
        else:
            visible_builtin_definitions = [
                _TabDefinition(
                    definition.key,
                    definition.title,
                    definition.page,
                    definition.search_controller,
                    definition.global_search_only,
                )
                for definition in self._visible_builtin_tab_definitions()
                if not definition.global_search_only
            ]
            visible_plugins, hidden_plugins = self._split_visible_and_hidden_plugin_tabs()
            placeholder_definition = self._startup_plugin_placeholder_definition()
            if placeholder_definition is not None:
                self._hidden_plugin_tab_definitions = []
                self.plugin_overflow_button.hide()
                self._close_plugin_overflow_drawer()
                definitions = [*visible_builtin_definitions, placeholder_definition]
            else:
                self._hidden_plugin_tab_definitions = hidden_plugins
                self.plugin_overflow_button.setVisible(bool(hidden_plugins))
                self.plugin_overflow_button.setText(f"更多({len(hidden_plugins)})" if hidden_plugins else "更多")
                if not hidden_plugins:
                    self._close_plugin_overflow_drawer()
                definitions = [*visible_builtin_definitions, *visible_plugins]

        self.nav_tabs.blockSignals(True)
        self.nav_tabs.clear()
        for definition in definitions:
            self.nav_tabs.addTab(definition.page, title_overrides.get(definition.key, definition.title))
        self.nav_tabs.blockSignals(False)

        if not self._global_search_active and self._restore_pending_tab_selection(definitions):
            self._sync_plugin_overflow_drawer()
            return
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

    def _restore_pending_tab_selection(self, definitions: list[_TabDefinition]) -> bool:
        pending_key = self._startup_pending_tab_restore_key
        if not pending_key:
            return False
        for index, definition in enumerate(definitions):
            if definition.key == pending_key:
                self.nav_tabs.setCurrentIndex(index)
                return True
        return False

    def _restore_saved_tab_selection(self, definitions: list[_TabDefinition]) -> bool:
        selected_key = getattr(self.config, "last_selected_tab", "douban") or "douban"
        for index, definition in enumerate(definitions):
            if definition.key == selected_key:
                self.nav_tabs.setCurrentIndex(index)
                return True
        hidden_builtin_definition = self._builtin_tab_definition_by_key(selected_key)
        if hidden_builtin_definition is not None:
            self.nav_tabs.setCurrentWidget(hidden_builtin_definition.page)
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
        self._remember_selected_category_key(selected_key, category_id)

    def _remember_selected_category_key(self, selected_key: str, category_id: str) -> None:
        if not selected_key or not category_id:
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
        if self._global_search_popup is None or not self._global_search_popup.isVisible():
            return
        pos = self.global_search_container.mapToGlobal(QPoint(0, self.global_search_container.height() + 4))
        self._global_search_popup.show_at(pos, self.global_search_container.width())

    def _ensure_global_search_popup(self) -> GlobalSearchPopup:
        if self._global_search_popup is None:
            self._global_search_popup = GlobalSearchPopup()
            self._global_search_popup.setObjectName("globalSearchPopup")
            self._global_search_popup.setWindowTitle("全局搜索")
            self._global_search_popup.hide()
            self._global_search_popup.item_clicked.connect(self._handle_global_search_popup_item_clicked)
            self._global_search_popup.heat_item_clicked.connect(self._handle_global_search_popup_heat_item_clicked)
            self._global_search_popup.clear_history_requested.connect(self._clear_global_search_history)
            self._global_search_popup.delete_history_requested.connect(self._delete_global_search_history)
            self._global_search_popup.hot_source_changed.connect(self._handle_global_search_hot_source_changed)
            self._global_search_popup.hot_tab_changed.connect(self._handle_global_search_hot_tab_changed)
            self._global_search_popup._apply_theme()
        return self._global_search_popup

    def _show_global_search_popup(self) -> None:
        popup = self._ensure_global_search_popup()
        self._render_global_search_popup()
        self._refresh_heat_recommendations()
        pos = self.global_search_container.mapToGlobal(QPoint(0, self.global_search_container.height() + 4))
        popup.show_at(pos, self.global_search_container.width())

    def _hide_global_search_popup(self) -> None:
        if self._global_search_popup is not None:
            self._global_search_popup.hide()

    def _dismiss_global_search_popup(self) -> None:
        self._hide_global_search_popup()

    def _dismiss_visible_global_search_popup(self) -> None:
        if self._global_search_popup is not None and self._global_search_popup.isVisible():
            self._dismiss_global_search_popup()

    @staticmethod
    def _widget_contains_global_pos(widget: QWidget, global_pos: QPoint) -> bool:
        if widget is None or not widget.isVisible():
            return False
        local_pos = widget.mapFromGlobal(global_pos)
        return widget.rect().contains(local_pos)

    def _handle_global_search_global_mouse_press(self, global_pos: QPoint) -> None:
        if self._global_search_popup is None or not self._global_search_popup.isVisible():
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
        updated = [normalized_keyword, *[item for item in previous if item != normalized_keyword]][
            :_GLOBAL_SEARCH_HISTORY_LIMIT
        ]
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

    def _handle_global_search_popup_heat_item_clicked(self, item: object) -> None:
        self._hide_global_search_popup()
        if self._heat_recommendations_dialog is not None:
            self._heat_recommendations_dialog.hide()
        if self._media_detail_controller is not None:
            self.open_media_detail_from_heat(item)
            return
        title = str(getattr(item, "title", "") or "").strip()
        if not title:
            return
        self.global_search_edit.setText(title)
        self._start_global_search(heat_source_kind="heat_recommendation")

    def _record_heat_media_event(
        self,
        event_type: str,
        media,
        *,
        context: dict[str, object] | None = None,
    ) -> None:
        heat_controller = self._heat_controller
        if media is None or heat_controller is None or not hasattr(heat_controller, "record_media_event"):
            return
        try:
            heat_controller.record_media_event(event_type, media, context=context or {})
        except Exception:
            pass

    def _ensure_heat_recommendations_dialog(self) -> HeatRecommendationsDialog:
        if self._heat_recommendations_dialog is None:
            self._heat_recommendations_dialog = HeatRecommendationsDialog(self)
            self._heat_recommendations_dialog.item_clicked.connect(
                self.open_media_detail_from_heat
            )
        return self._heat_recommendations_dialog

    def _show_heat_recommendations_dialog(self) -> None:
        self._hide_global_search_popup()
        dialog = self._ensure_heat_recommendations_dialog()
        dialog.set_loading()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._refresh_heat_recommendations()

    def _toggle_global_search_popup(self) -> None:
        if self._global_search_popup is not None and self._global_search_popup.isVisible():
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
        if self._global_search_popup is None:
            return
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
            self._global_search_history()[:_GLOBAL_SEARCH_HISTORY_DISPLAY_LIMIT],
            self._global_search_hotkey_active_source,
            self._global_search_hot_sources(),
            self._global_search_hotkey_active_type,
            hot_categories,
            hotkeys,
        )

    def _refresh_heat_recommendations(self) -> None:
        if self._heat_controller is None:
            if self._heat_recommendations_dialog is not None:
                self._heat_recommendations_dialog.set_items([])
            return
        self._heat_recommendation_request_id += 1
        request_id = self._heat_recommendation_request_id

        def run() -> None:
            try:
                items = list(
                    self._heat_controller.load_recommendations(
                        limit=_HEAT_RECOMMENDATION_DISPLAY_LIMIT
                    )
                )
            except Exception:
                items = []
            items = self._fill_heat_recommendations_from_douban_tv(items)
            if self._is_window_alive():
                self._global_search_popup_signals.heat_recommendations_loaded.emit(
                    request_id,
                    items,
                )

        threading.Thread(target=run, daemon=True).start()

    def _fill_heat_recommendations_from_douban_tv(self, items: list[Any]) -> list[Any]:
        merged = list(items)[:_HEAT_RECOMMENDATION_DISPLAY_LIMIT]
        if len(merged) >= _HEAT_RECOMMENDATION_DISPLAY_LIMIT:
            return merged
        controller = getattr(self.douban_page, "controller", None)
        if controller is None:
            return merged
        category_id = self._douban_tv_recommendation_category_id(controller)
        if not category_id:
            return merged
        try:
            douban_items, _total = self._load_controller_items(controller, category_id, 1)
        except Exception:
            return merged

        seen_titles = {
            str(
                getattr(item, "title", "") or getattr(item, "vod_name", "") or ""
            ).strip()
            for item in merged
        }
        for vod in douban_items:
            if len(merged) >= _HEAT_RECOMMENDATION_DISPLAY_LIMIT:
                break
            title = str(getattr(vod, "vod_name", "") or "").strip()
            if not title or title in seen_titles:
                continue
            recommendation = self._heat_recommendation_from_douban_vod(vod)
            if recommendation is None:
                continue
            merged.append(recommendation)
            seen_titles.add(title)
        return merged

    def _douban_tv_recommendation_category_id(self, controller: Any) -> str:
        load_categories = getattr(controller, "load_categories", None)
        if not callable(load_categories):
            return ""
        try:
            categories = list(load_categories())
        except Exception:
            return ""
        if not categories:
            return ""
        preferred_terms = ("热门电视剧", "电视剧", "剧集", "剧场", "tv")
        for term in preferred_terms:
            for category in categories:
                category_id = str(getattr(category, "type_id", "") or "").strip()
                category_title = str(getattr(category, "type_name", "") or "").strip()
                haystack = f"{category_id} {category_title}".casefold()
                if category_id and term.casefold() in haystack:
                    return category_id
        return ""

    @staticmethod
    def _heat_recommendation_from_douban_vod(vod: Any) -> HeatRecommendation | None:
        identity = heat_identity_from_vod(vod)
        if identity is None:
            return None
        return HeatRecommendation(
            media_key=identity.media_key,
            title=identity.title,
            poster=identity.poster,
            year=identity.year,
            media_type="tv",
            external_ids=dict(identity.external_ids),
            reason="豆瓣热门电视剧",
        )

    def _handle_heat_recommendations_loaded(self, request_id: int, items: object) -> None:
        if request_id != self._heat_recommendation_request_id:
            return
        if not isinstance(items, Iterable):
            items = []
        loaded_items = list(items)
        if self._global_search_popup is not None:
            self._global_search_popup.set_heat_items(loaded_items)
        if self._heat_recommendations_dialog is not None:
            self._heat_recommendations_dialog.set_items(loaded_items)

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
        if self._startup_pending_tab_restore_key:
            visible_keys = {definition.key for definition in self._visible_tab_definitions()}
            if selected_key == self._startup_pending_tab_restore_key:
                self._startup_pending_tab_restore_key = ""
            elif self.isVisible() and self._startup_pending_tab_restore_key in visible_keys and selected_key is not None:
                self._startup_pending_tab_restore_key = ""
        if (
            self._startup_plugin_pending_tab_restore_key
            and self._startup_plugin_load_started
            and self._startup_plugin_load_state == "loading"
            and widget is not self._startup_plugin_placeholder_page
            and selected_key != self._startup_plugin_pending_tab_restore_key
            and selected_key is not None
            and selected_key.startswith("plugin:")
        ):
            self._startup_plugin_pending_tab_restore_key = ""
        self._sync_plugin_overflow_drawer()
        if self._global_search_active:
            return
        if (getattr(self.config, "home_mode", "browse") or "browse") != "classic":
            self._remember_selected_tab(widget)
        if isinstance(widget, PosterGridPage):
            self._remember_selected_category(widget, widget.selected_category_id)
        if widget is self.douban_page:
            self.douban_page.ensure_loaded()
            return
        if widget is self.global_catalog_page:
            self.global_catalog_page.ensure_loaded()
            return
        if widget is self.telegram_page:
            self.telegram_page.ensure_loaded()
            return
        if widget is self.telegram_channel_page and self.telegram_channel_page is not None:
            self.telegram_channel_page.ensure_loaded()
            return
        if widget is self.bilibili_page and self.bilibili_page is not None:
            self.bilibili_page.ensure_loaded()
            return
        if widget is self.youtube_page and self.youtube_page is not None:
            self.youtube_page.ensure_loaded()
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
        if widget is self.browse_page:
            if hasattr(self.browse_controller, "load_folder"):
                self.browse_page.ensure_loaded(self.config.last_path or "/")
            return
        if widget is self.favorites_page:
            if hasattr(self.favorites_page.controller, "load_page"):
                self.favorites_page.ensure_loaded()
            return
        if widget is self.following_page:
            if hasattr(self.following_page.controller, "load_page"):
                self.following_page.ensure_loaded()
            return
        if widget is self.history_page:
            if hasattr(self.history_page.controller, "load_page"):
                self.history_page.ensure_loaded()
            return
        for page, _controller, _plugin_id in self._plugin_pages:
            if widget is page:
                page.ensure_loaded()
                return

    def _overflow_drawer_items(self) -> list[tuple[str, str, bool]]:
        active_key = self._tab_key_for_widget(self._active_widget or self.nav_tabs.currentWidget())
        title_overrides = self._global_search_title_overrides() if self._global_search_active else {}
        return [
            (definition.key, title_overrides.get(definition.key, definition.title), definition.key == active_key)
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
        self._sync_plugin_overflow_active_state()
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
            self._sync_plugin_overflow_active_state()
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

    def _sync_plugin_overflow_active_state(self) -> None:
        active_key = self._tab_key_for_widget(self._active_widget or self.nav_tabs.currentWidget())
        hidden_plugin_active = any(
            definition.key == active_key
            for definition in self._hidden_plugin_tab_definitions
        )
        hidden_builtin_active = (
            active_key is not None
            and active_key in getattr(self, "_builtin_hidden_keys", set())
        )
        self.plugin_overflow_button.setChecked(hidden_plugin_active)
        self._set_dynamic_property(self.nav_tabs.tab_bar, "hiddenPluginActive", hidden_plugin_active)
        self._set_dynamic_property(self.nav_tabs.tab_bar, "hiddenTabActive", hidden_plugin_active or hidden_builtin_active)

    @staticmethod
    def _set_dynamic_property(widget: QWidget, name: str, value: object) -> None:
        if widget.property(name) == value:
            return
        widget.setProperty(name, value)
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()

    def _handle_hidden_plugin_selected(self, plugin_key: str) -> None:
        candidates = self._all_tab_definitions() if self._global_search_active else self._plugin_tab_definitions
        definition = next((item for item in candidates if item.key == plugin_key), None)
        if definition is None:
            return
        self.nav_tabs.setCurrentWidget(definition.page)

    def _open_builtin_page_from_header(self, tab_key: str) -> None:
        definition = self._builtin_tab_definition_by_key(tab_key)
        if definition is None:
            return
        if hasattr(self, "_classic_home_page"):
            self._classic_home_page.select_source_key(tab_key, emit=False)
            if self.config.last_selected_tab != tab_key:
                self.config.last_selected_tab = tab_key
                self._save_config()
        hide_navigation = (getattr(self, "_active_home_mode", getattr(self.config, "home_mode", "browse")) or "browse") != "browse"
        self._show_builtin_page_in_home_stack(definition, hide_navigation=hide_navigation)

    def _return_to_configured_home(self) -> None:
        self._dismiss_visible_global_search_popup()
        self.apply_home_mode(getattr(self.config, "home_mode", "browse") or "browse")

    def _show_builtin_page_in_home_stack(
        self,
        definition: _BuiltinTabDefinition,
        *,
        hide_navigation: bool = True,
    ) -> None:
        self.nav_tabs.setNavigationVisible(not hide_navigation)
        self.nav_tabs.setVisible(True)
        self._home_stack.setCurrentWidget(self.nav_tabs)
        self.nav_tabs.setCurrentWidget(definition.page)

    def _handle_tab_context_menu_requested(self, pos: QPoint) -> None:
        index = self.nav_tabs.tab_bar.tabAt(pos)
        if index < 0:
            return
        widget = self.nav_tabs.widget(index)
        tab_key = self._tab_key_for_widget(widget)
        if not tab_key:
            return
        global_pos = self.nav_tabs.tab_bar.mapToGlobal(pos)
        if tab_key.startswith("plugin:"):
            self._open_plugin_context_menu(tab_key.removeprefix("plugin:"), global_pos)
            return
        if self._builtin_tab_definition_by_key(tab_key) is not None:
            self._open_builtin_tab_context_menu(tab_key, global_pos)

    def _current_builtin_overrides(self):
        return parse_builtin_tab_overrides_json(getattr(self.config, "builtin_tab_overrides_json", ""))

    def _default_builtin_tab_keys(self) -> list[str]:
        return [definition.key for definition in self._builtin_tab_default_definitions()]

    def _save_builtin_overrides_object(self, overrides: BuiltinTabOverrides) -> None:
        self._handle_builtin_tab_overrides_saved(dumps_builtin_tab_overrides_json(overrides))

    def _open_builtin_tab_context_menu(self, tab_key: str, global_pos: QPoint) -> None:
        self._dismiss_visible_global_search_popup()
        definition = self._builtin_tab_definition_by_key(tab_key)
        if definition is None:
            return
        menu = QMenu(self)
        rename_action = menu.addAction("重命名")
        hide_action = menu.addAction("隐藏")
        chosen = menu.exec(global_pos)
        if chosen is rename_action:
            self._rename_builtin_tab_from_context(tab_key, definition.title)
        elif chosen is hide_action:
            self._hide_builtin_tab_from_context(tab_key)

    def _rename_builtin_tab_from_context(self, tab_key: str, current_title: str) -> None:
        value, accepted = QInputDialog.getText(self, "重命名内置源", "显示名称", text=current_title)
        value = value.strip() if accepted else ""
        if not value:
            return
        overrides = self._current_builtin_overrides()
        if not overrides.order:
            overrides.order = self._default_builtin_tab_keys()
        overrides.renames[tab_key] = value
        self._save_builtin_overrides_object(overrides)

    def _hide_builtin_tab_from_context(self, tab_key: str) -> None:
        overrides = self._current_builtin_overrides()
        if not overrides.order:
            overrides.order = self._default_builtin_tab_keys()
        if tab_key not in overrides.hidden:
            overrides.hidden.append(tab_key)
        self._save_builtin_overrides_object(overrides)

    def _select_first_visible_tab(self) -> None:
        if self.nav_tabs.count() > 0:
            first_widget = self.nav_tabs.widget(0)
            if first_widget is not None:
                self.nav_tabs.setCurrentWidget(first_widget)

    def _open_plugin_context_menu(self, plugin_key: str, global_pos: QPoint) -> None:
        self._dismiss_visible_global_search_popup()
        plugin_id = self._normalize_plugin_id(plugin_key)
        if self._plugin_actions is None:
            return
        menu = QMenu(self)
        reload_action = menu.addAction("重新加载")
        rename_action = menu.addAction("编辑名称")
        config_action = menu.addAction("编辑配置")
        manage_categories_action = menu.addAction("分类管理")
        toggle_action = menu.addAction(self._plugin_toggle_action_text(plugin_id))
        chosen = menu.exec(global_pos)
        if chosen is reload_action:
            self._run_plugin_context_action("refresh", plugin_id)
        elif chosen is rename_action:
            self._run_plugin_context_action("rename", plugin_id)
        elif chosen is config_action:
            self._run_plugin_context_action("edit_config", plugin_id)
        elif chosen is manage_categories_action:
            self._run_plugin_context_action("manage_categories", plugin_id)
        elif chosen is toggle_action:
            self._run_plugin_context_action("toggle_enabled", plugin_id)

    def _connect_video_item_context_menu(self, page: PosterGridPage) -> None:
        page.card_context_menu_requested.connect(
            lambda item, global_pos, current_page=page: self._open_video_item_context_menu(
                current_page,
                item,
                global_pos,
            )
        )

    def _open_video_item_context_menu(self, page: PosterGridPage, item, global_pos: QPoint) -> None:
        if item is None:
            return
        self._dismiss_visible_global_search_popup()
        action_ids = self._video_item_context_menu_actions(page)
        if not action_ids:
            return
        menu = QMenu(self)
        open_action = menu.addAction("打开播放") if "open" in action_ids else None
        search_action = menu.addAction("全局搜索") if "search" in action_ids else None
        favorite_action = menu.addAction("加入收藏") if "favorite" in action_ids else None
        chosen = menu.exec(global_pos)
        if chosen is open_action:
            self._handle_video_item_context_open(page, item)
        elif chosen is search_action:
            self._handle_video_item_context_global_search(item)
        elif chosen is favorite_action:
            self._handle_video_item_context_favorite(page, item)

    def _video_item_context_menu_actions(self, page: PosterGridPage) -> list[str]:
        is_douban_page = page is self.douban_page
        is_live_page = page is self.live_page
        is_external_results = bool(getattr(page, "_external_results_active", False))
        action_ids: list[str] = []
        if not is_douban_page:
            action_ids.append("open")
        if not is_live_page and not is_external_results:
            action_ids.append("search")
        if not is_douban_page:
            action_ids.append("favorite")
        return action_ids

    def _handle_video_item_context_open(self, page: PosterGridPage, item) -> None:
        page._handle_card_clicked(item)

    def _handle_video_item_context_global_search(self, item) -> None:
        keyword = str(getattr(item, "vod_name", "") or "").strip()
        if not keyword:
            return
        self._handle_favorite_global_search(keyword)

    def _handle_favorite_global_search(self, keyword: str) -> None:
        keyword = str(keyword or "").strip()
        if not keyword:
            return
        self.global_search_edit.setText(keyword)
        self._start_global_search()

    def _handle_video_item_context_favorite(self, page: PosterGridPage, item) -> None:
        payload = self._video_item_favorite_payload(page, item)
        if payload is None:
            return
        self._favorites_controller.add_favorite(payload)
        self._record_heat_media_event(
            "favorite_add",
            heat_identity_from_vod(item),
            context={
                "source_kind": str(payload.get("source_kind") or ""),
                "source_key": str(payload.get("source_key") or ""),
            },
        )

    def _video_item_favorite_source(self, page: PosterGridPage) -> tuple[str, str, str] | None:
        static_sources = {
            self.telegram_page: ("telegram", "", "电报影视"),
            self.telegram_channel_page: ("telegram_channel", "", "电报频道"),
            self.live_page: ("live", "", "网络直播"),
            self.emby_page: ("emby", "", "Emby"),
            self.bilibili_page: ("bilibili", "", "B站"),
            self.youtube_page: ("youtube", "", "YouTube"),
            self.jellyfin_page: ("jellyfin", "", "Jellyfin"),
            self.feiniu_page: ("feiniu", "", "飞牛影视"),
        }
        source = static_sources.get(page)
        if source is not None:
            return source
        for plugin_page, _controller, plugin_id in self._plugin_pages:
            if page is plugin_page:
                title = next(
                    (definition.title for definition in self._plugin_tab_definitions if definition.key == f"plugin:{plugin_id}"),
                    "插件",
                )
                return ("plugin", plugin_id, title)
        return None

    def _video_item_favorite_payload(self, page: PosterGridPage, item) -> dict[str, object] | None:
        source = self._video_item_favorite_source(page)
        if source is None:
            return None
        vod_id = str(getattr(item, "vod_id", "") or "").strip()
        if not vod_id:
            return None
        source_kind, source_key, source_name = source
        title = str(getattr(item, "vod_name", "") or "").strip()
        now = int(time.time())
        return {
            **self._favorite_tmdb_identity_payload(item, None),
            "source_kind": source_kind,
            "source_key": source_key,
            "source_name": source_name,
            "vod_id": vod_id,
            "vod_name_snapshot": title,
            "latest_vod_name": title,
            "vod_pic": str(getattr(item, "vod_pic", "") or ""),
            "vod_remarks": str(getattr(item, "vod_remarks", "") or ""),
            "title_changed": False,
            "created_at": now,
            "updated_at": now,
        }

    def _favorite_tmdb_identity_payload(self, vod: VodItem, item: PlayItem | None) -> dict[str, str]:
        tmdb_id = ""
        for field in [*list(vod.detail_fields or []), *list(getattr(item, "detail_fields", []) or [])]:
            label = str(getattr(field, "label", "") or "").strip().lower()
            value = str(getattr(field, "value", "") or "").strip()
            if "tmdb" not in label or not value:
                continue
            tmdb_id = value
            break
        if not tmdb_id:
            return {}
        media_tokens = " ".join(
            str(value or "").strip().lower()
            for value in (vod.type_name, vod.category_name)
            if str(value or "").strip()
        )
        media_type = "tv"
        if any(token in media_tokens for token in ("电影", "影片", "movie")):
            media_type = "movie"
        return {
            "tmdb_provider_id": f"{media_type}:{tmdb_id}",
            "tmdb_id": tmdb_id,
            "tmdb_media_type": media_type,
        }

    def _open_plugin_category_manager(self, plugin_id: int) -> bool:
        if self._plugin_manager is None:
            return False
        self._close_plugin_overflow_drawer()
        dialog = PluginCategoryManagerDialog(self._plugin_manager, plugin_id, self)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _run_plugin_context_action(self, action_name: str, plugin_id: str) -> bool:
        if self._plugin_actions is None:
            return False
        normalized_plugin_id = int(self._normalize_plugin_id(plugin_id))
        if action_name == "manage_categories":
            changed = self._open_plugin_category_manager(normalized_plugin_id)
            if not changed:
                return False
            self._reload_changed_plugin_tabs([str(normalized_plugin_id)])
            self._sync_plugin_overflow_drawer(reset_search=False)
            return True
        action_map = {
            "refresh": self._plugin_actions.refresh_plugin,
            "rename": self._plugin_actions.rename_plugin,
            "edit_config": self._plugin_actions.edit_plugin_config,
            "toggle_enabled": self._plugin_actions.toggle_plugin_enabled,
        }
        action = action_map.get(action_name)
        if action is None:
            return False
        result = action(self, normalized_plugin_id)
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
        self._skip_next_telegram_open_request_vod_id = str(vod_id or "")
        title = str(getattr(item, "vod_name", "") or "")
        # Drive resolve can take seconds (temp-share mount + listing), so show the player
        # window immediately and fill in the playlist when the request resolves.
        self._open_player_immediately(
            self._build_placeholder_player_request(item, source_kind="telegram")
        )

        def build_request() -> OpenPlayerRequest:
            request = self.telegram_controller.build_request(vod_id, title)
            return self._apply_request_fallback_metadata(request, item, prefer_fallback_media_title=True)

        self._start_open_request(build_request)

    def _handle_telegram_open_requested(self, vod_id: str) -> None:
        normalized_vod_id = str(vod_id or "")
        if normalized_vod_id and normalized_vod_id == self._skip_next_telegram_open_request_vod_id:
            self._skip_next_telegram_open_request_vod_id = ""
            return
        self._start_open_request(lambda: self.telegram_controller.build_request(vod_id))

    def _handle_telegram_channel_item_open_requested(self, item) -> None:
        vod_id = item.vod_id
        title = str(getattr(item, "vod_name", "") or "")
        # Drive resolve can take seconds, so show the player window immediately.
        self._open_player_immediately(
            self._build_placeholder_player_request(item, source_kind="telegram_channel")
        )
        self._start_open_request(lambda: self.telegram_channel_controller.build_request(vod_id, title))

    def _handle_bilibili_item_open_requested(self, item) -> None:
        if getattr(item, "vod_tag", "") == "folder":
            if self.bilibili_page is not None:
                self._open_media_folder(self.bilibili_page, self.bilibili_controller, item)
            return
        vod_id = item.vod_id
        self._start_open_request(lambda: self.bilibili_controller.build_request(vod_id))

    def _handle_youtube_item_open_requested(self, item) -> None:
        self._start_youtube_item_open(self.youtube_controller, item)

    def _start_youtube_item_open(self, controller: Any, item: Any) -> None:
        """Open a YouTube card immediately, then resolve its playable request off-thread."""
        vod_id = str(getattr(item, "vod_id", "") or "").strip()
        if vod_id and vod_id == self._youtube_open_request_vod_id:
            self._append_player_status_log("详情仍在加载中...")
            return
        self._youtube_open_request_vod_id = vod_id

        # Building a request may invoke yt-dlp (especially for playlists/channels),
        # so never make it part of the click handler. The placeholder carries the
        # card's title and poster and is deliberately opened before the worker starts.
        placeholder_request = self._build_placeholder_player_request(item, source_kind="youtube")
        self._open_player_immediately(placeholder_request)

        fast_request_builder = getattr(controller, "build_request_from_item", None)

        def build_request() -> OpenPlayerRequest:
            if callable(fast_request_builder):
                request = fast_request_builder(item)
            else:
                request = controller.build_request(vod_id)
            return self._apply_request_fallback_metadata(request, item)

        self._youtube_open_request_id = self._start_open_request(build_request)

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

    def _searchable_tab_definitions(
        self,
        *,
        include_smart_search: bool = True,
    ) -> list[_TabDefinition]:
        return [
            definition
            for definition in self._all_tab_definitions()
            if definition.search_controller is not None
            and (include_smart_search or definition.key != "smart:search")
        ]

    def _build_drive_detail_request(self, link: str) -> OpenPlayerRequest:
        # Try the new per-directory resolve; on any failure or empty result (including an
        # older backend without /api/drive) fall back to the legacy flattened loader.
        if self._drive_resolver is not None:
            try:
                result = self._drive_resolver(link) or {}
                resource_id = str(result.get("resourceId") or "")
                vod_name = str(result.get("vodName") or link)
                detail = VodItem(vod_id=link, vod_name=vod_name)
                source_groups, playlists = build_drive_grouped_sources(
                    detail,
                    resource_id,
                    result.get("directories") or [],
                    result.get("files") or [],
                    self._drive_files_loader,
                )
                if source_groups and any(playlists):
                    return OpenPlayerRequest(
                        vod=detail,
                        playlist=playlists[0],
                        clicked_index=0,
                        playlists=playlists,
                        source_groups=source_groups,
                        source_group_index=0,
                        source_index=0,
                        source_kind="telegram",
                        source_mode="detail",
                        source_vod_id=link,
                        detail_resolver=None,
                        drive_resource_id=resource_id,
                        drive_files_loader=self._drive_files_loader,
                    )
            except Exception:
                pass
        return self._build_drive_detail_request_legacy(link)

    def _build_drive_detail_request_legacy(self, link: str) -> OpenPlayerRequest:
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

    def _build_drive_placeholder_player_request(self, link: str) -> OpenPlayerRequest:
        return OpenPlayerRequest(
            vod=VodItem(vod_id=link, vod_name=link),
            playlist=[],
            clicked_index=0,
            source_kind="telegram",
            source_mode="detail",
            source_vod_id=link,
            use_local_history=False,
            initial_log_message="正在加载详情...",
            is_placeholder=True,
        )

    def _build_offline_download_placeholder_player_request(self, link: str) -> OpenPlayerRequest:
        return OpenPlayerRequest(
            vod=VodItem(vod_id=link, vod_name=link),
            playlist=[],
            clicked_index=0,
            source_kind="browse",
            source_mode="detail",
            source_vod_id=link,
            use_local_history=False,
            initial_log_message="正在加载详情...",
            is_placeholder=True,
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
            selected_audio_track_id = current_item.selected_audio_track_id or ""
            if selected_quality_id.startswith("ytdlp_"):
                return yt_dlp.resolve_for_quality(
                    source_url,
                    selected_quality_id,
                    audio_track_id=selected_audio_track_id,
                )
            return yt_dlp.resolve(
                source_url,
                max_height=None,
                selected_audio_track_id=selected_audio_track_id,
            )

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
        playback_format_selector = getattr(self._yt_dlp_service, "playback_format_selector", None)
        startup_ytdl_format = (
            playback_format_selector()
            if callable(playback_format_selector)
            else "bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best"
        )

        def load_item(session, current_item: PlayItem):
            source_url = (current_item.original_url or current_item.vod_id or url).strip() or url
            selected_quality_id = current_item.selected_playback_quality_id or ""
            selected_audio_track_id = current_item.selected_audio_track_id or ""
            current_url = str(current_item.url or "").strip()
            full_metadata = bool(current_url and current_url != source_url)
            if (
                (not current_url or current_url == source_url)
                and not selected_audio_track_id
                and not selected_quality_id.startswith("ytdlp_")
                and hasattr(self._yt_dlp_service, "resolve_fast")
            ):
                fast_or_full = getattr(self._yt_dlp_service, "resolve_fast_or_full", None)
                if callable(fast_or_full):
                    result = fast_or_full(source_url)
                else:
                    result = self._yt_dlp_service.resolve_fast(source_url)
            elif selected_quality_id.startswith("ytdlp_"):
                resolver = (
                    self._yt_dlp_service.resolve_for_quality_full
                    if full_metadata and hasattr(self._yt_dlp_service, "resolve_for_quality_full")
                    else self._yt_dlp_service.resolve_for_quality
                )
                result = resolver(
                    source_url,
                    selected_quality_id,
                    audio_track_id=selected_audio_track_id,
                )
            elif full_metadata and hasattr(self._yt_dlp_service, "resolve_full"):
                result = self._yt_dlp_service.resolve_full(
                    source_url,
                    max_height=None,
                    selected_audio_track_id=selected_audio_track_id,
                )
            else:
                result = self._yt_dlp_service.resolve(
                    source_url,
                    max_height=None,
                    selected_audio_track_id=selected_audio_track_id,
                )
            self._yt_dlp_service.apply_result(
                result,
                vod=session.vod,
                item=current_item,
                source_url=source_url,
            )
            return None

        item = PlayItem(
            title=url,
            url=url,
            original_url=url,
            vod_id=url,
            media_title=url,
            selected_playback_quality_id="",
            ytdl_format=startup_ytdl_format,
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
        return DirectParseDanmakuController(
            load=self._direct_parse_danmaku_loader,
            config_loader=lambda: self.config,
            danmaku_preference_store=self._danmaku_preference_store,
        )

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
            self._open_player_immediately(self._build_offline_download_placeholder_player_request(keyword))
            self._start_open_request(lambda: self._build_offline_download_request(keyword))
            return True
        if not _looks_like_http_url(keyword):
            return False
        if _looks_like_drive_share_link(keyword):
            self._open_player_immediately(self._build_drive_placeholder_player_request(keyword))
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

    def _start_global_search(
        self,
        *,
        include_smart_search: bool = True,
        heat_source_kind: str = "global_search",
    ) -> None:
        keyword = self.global_search_edit.text().strip()
        if not keyword:
            return
        self._hide_global_search_popup()
        if self._start_direct_open_from_global_search(keyword):
            return
        self._record_global_search_history(keyword)
        heat_controller = self._heat_controller
        if heat_controller is not None and hasattr(heat_controller, "record_search"):
            try:
                heat_controller.record_search(keyword, source_kind=heat_source_kind)
            except Exception:
                pass
        active_home_mode = getattr(self, "_active_home_mode", getattr(self.config, "home_mode", "browse")) or "browse"
        if active_home_mode == "classic":
            self._ensure_classic_global_search_plugins_loaded()
        searchable = self._searchable_tab_definitions(
            include_smart_search=include_smart_search,
        )
        if not searchable:
            self.global_search_status_label.setText("无可搜索来源")
            return
        self._global_search_request_id += 1
        request_id = self._global_search_request_id
        self._global_search_restore_plugin_keys = [
            key
            for key in (self._tab_key_for_widget(self.nav_tabs.widget(index)) for index in range(self.nav_tabs.count()))
            if key is not None and key.startswith("plugin:")
        ]
        self._global_search_active = True
        self._global_search_in_progress = True
        self._global_search_keyword = keyword
        self._global_search_results = {}
        self._global_search_pending_keys = {definition.key for definition in searchable}
        self.global_search_status_label.setText("搜索中...")
        if self._home_stack.currentWidget() is not self.nav_tabs:
            self.nav_tabs.setNavigationVisible(True)
            self.nav_tabs.setVisible(True)
            self._home_stack.setCurrentWidget(self.nav_tabs)
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
            items, page_count = controller.search_items(keyword, page_number)
        except Exception:
            if self._is_window_alive():
                self._global_search_signals.failed.emit(request_id, definition.key)
            return
        total_count = max(0, int(getattr(page_count, "total", page_count) or 0))
        normalized_page_count = max(0, int(page_count or 0))
        if self._is_window_alive():
            self._global_search_signals.succeeded.emit(
                request_id,
                _GlobalSearchResult(
                    key=definition.key,
                    title=definition.title,
                    page=definition.page,
                    items=list(items),
                    total_count=total_count,
                    page_count=normalized_page_count,
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
            page = definition.page
            if isinstance(page, (PosterGridPage, HistoryPage, FavoritesPage, FollowingPage)):
                page.clear_external_results()
        self._refresh_visible_tabs()
        self._global_search_restore_plugin_keys = []
        self._sync_global_search_action_state()
        self._hide_global_search_popup()
        home_mode = getattr(self, "_active_home_mode", getattr(self.config, "home_mode", "browse")) or "browse"
        if home_mode == "simplified":
            self._return_to_simplified_home_after_global_search_clear()
        elif home_mode == "media":
            self._apply_media_home_mode()
        elif home_mode == "classic":
            self._apply_classic_home_mode()

    def _return_to_simplified_home_after_global_search_clear(self) -> None:
        page = self._ensure_simplified_home_page()
        page.search_edit.blockSignals(True)
        page.search_edit.clear()
        page.search_edit.blockSignals(False)
        page._sync_search_button()
        self._apply_simplified_home_mode()

    def _show_global_search_result(self, result: _GlobalSearchResult) -> None:
        page_loader = self._build_global_search_page_loader(result.key)
        if isinstance(result.page, HistoryPage):
            result.page.show_external_results(
                result.items,
                result.page_count,
                page=result.page_number,
                page_loader=page_loader,
            )
        elif isinstance(result.page, (FavoritesPage, FollowingPage)):
            result.page.show_external_results(
                result.items,
                result.page_count,
                page=result.page_number,
                empty_message="无搜索结果",
                page_loader=page_loader,
            )
        else:
            cast(PosterGridPage, result.page).show_external_results(
                result.items,
                result.page_count,
                page=result.page_number,
                empty_message="无搜索结果",
                page_loader=page_loader,
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
            folder_navigation_enabled=True,
            filter_panel_state=self._filter_panel_state,
            parent=self.nav_tabs.content_stack,
        )
        page.item_open_requested.connect(
            lambda item, controller=controller, plugin_id=plugin_id: self._open_spider_item(
                controller,
                plugin_id,
                item,
            )
        )
        page.folder_breadcrumb_requested.connect(
            lambda node_id, kind, index, page=page, controller=controller: self._handle_media_breadcrumb_requested(
                page,
                controller,
                node_id,
                kind,
                index,
            )
        )
        self._connect_video_item_context_menu(page)
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

        active_key = self._tab_key_for_widget(self._active_widget or self.nav_tabs.currentWidget())
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
        if active_key is not None:
            active_definition = next((definition for definition in ordered_tabs if definition.key == active_key), None)
            if active_definition is not None:
                self._active_widget = active_definition.page
        self._refresh_visible_tabs()
        self._refresh_classic_source_entries_if_active()
        return True

    def _rebuild_spider_plugin_tabs(self) -> None:
        for page, _controller, _plugin_id in self._plugin_pages:
            page.deleteLater()
        self._plugin_pages = []
        self._plugin_tab_definitions = []
        for definition in self._plugin_definitions:
            self._append_plugin_page_from_definition(definition)
        if not self._defer_navigation_refresh:
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
    def _should_override_request_vod_name_with_fallback(current_name: str, fallback_name: str) -> bool:
        if not current_name or not fallback_name:
            return False
        current_has_cjk = re.search(r"[\u3400-\u9fff]", current_name) is not None
        fallback_has_cjk = re.search(r"[\u3400-\u9fff]", fallback_name) is not None
        return fallback_has_cjk and not current_has_cjk

    @staticmethod
    def _apply_request_fallback_metadata(
        request: OpenPlayerRequest,
        item: Any,
        *,
        prefer_fallback_media_title: bool = False,
    ) -> OpenPlayerRequest:
        fallback_type_name = str(getattr(item, "type_name", "") or "").strip()
        fallback_category_name = str(getattr(item, "category_name", "") or "").strip()
        fallback_vod_name = str(getattr(item, "vod_name", "") or "").strip()
        if fallback_type_name and not request.vod.type_name:
            request.vod.type_name = fallback_type_name
        if fallback_category_name and not request.vod.category_name:
            request.vod.category_name = fallback_category_name
        if fallback_vod_name and (
            not request.vod.vod_name
            or (
                prefer_fallback_media_title
                and MainWindow._should_override_request_vod_name_with_fallback(request.vod.vod_name, fallback_vod_name)
            )
        ):
            request.vod.vod_name = fallback_vod_name
        resolved_media_title = (
            fallback_vod_name if prefer_fallback_media_title and fallback_vod_name else request.vod.vod_name or fallback_vod_name
        ).strip()
        playlists = list(request.playlists or [])
        if not playlists and request.playlist:
            playlists = [request.playlist]
        for playlist in playlists:
            for play_item in playlist:
                if fallback_type_name and not play_item.type_name:
                    play_item.type_name = fallback_type_name
                if fallback_category_name and not play_item.category_name:
                    play_item.category_name = fallback_category_name
                if resolved_media_title and (prefer_fallback_media_title or not play_item.media_title):
                    play_item.media_title = resolved_media_title
        return request

    @staticmethod
    def _apply_request_playback_history_title(request: OpenPlayerRequest) -> OpenPlayerRequest:
        history_loader = request.playback_history_loader
        if history_loader is None:
            return request
        try:
            history = history_loader()
        except Exception:
            return request
        if history is None or not str(getattr(history, "vod_name", "") or "").strip():
            return request
        return MainWindow._apply_request_fallback_metadata(request, history, prefer_fallback_media_title=True)

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

    def _start_global_search_from_msearch_item(self, item: Any) -> bool:
        vod_id = str(getattr(item, "vod_id", "") or "").strip()
        if not vod_id.startswith("msearch:"):
            return False
        keyword = str(getattr(item, "vod_name", "") or "").strip()
        if not keyword:
            return True
        self.global_search_edit.setText(keyword)
        self._start_global_search()
        return True

    def _open_spider_item(self, controller, plugin_id: str, item: Any) -> None:
        if self._start_global_search_from_msearch_item(item):
            return
        if getattr(item, "vod_tag", "") == "folder":
            page = None
            if (
                hasattr(self, "_classic_home_page")
                and self._classic_home_page.current_source_key() == f"plugin:{plugin_id}"
                and getattr(self._classic_home_page.grid_page, "controller", None) is controller
            ):
                page = self._classic_home_page.grid_page
            if page is None:
                page = next(
                    (
                        plugin_page
                        for plugin_page, plugin_controller, current_plugin_id in self._plugin_pages
                        if plugin_controller is controller and str(current_plugin_id) == str(plugin_id)
                    ),
                    None,
                )
            if page is not None:
                self._open_media_folder(page, controller, item)
            return
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
        dialog = PluginManagerDialog(
            self._plugin_manager,
            self,
            builtin_tabs=[
                {"key": definition.key, "title": definition.title}
                for definition in self._builtin_tab_default_definitions()
            ],
            builtin_tab_overrides_json=getattr(self.config, "builtin_tab_overrides_json", ""),
            save_builtin_tab_overrides=self._save_builtin_tab_overrides,
        )
        dialog.builtin_tabs_saved.connect(self._handle_builtin_tab_overrides_saved)
        dialog.exec()
        if bool(getattr(dialog, "builtin_tabs_dirty", False)):
            self._builtin_tab_definitions = self._build_builtin_tab_definitions()
            self._refresh_navigation_tabs()
        if not bool(getattr(dialog, "plugin_tabs_dirty", False)):
            return
        changed_plugin_ids = [str(plugin_id) for plugin_id in getattr(dialog, "changed_plugin_ids", []) if str(plugin_id)]
        if self._reload_changed_plugin_tabs(changed_plugin_ids):
            return
        self._plugin_definitions = self._load_plugin_definitions_with_manager("load_enabled_plugins")
        self._rebuild_spider_plugin_tabs()
        self._refresh_classic_source_entries_if_active()

    def _save_builtin_tab_overrides(self, payload: str) -> None:
        self.config.builtin_tab_overrides_json = payload
        self._save_config()

    def _handle_builtin_tab_overrides_saved(self, payload: str) -> None:
        if getattr(self.config, "builtin_tab_overrides_json", "") != payload:
            self.config.builtin_tab_overrides_json = payload
            self._save_config()
        self._builtin_tab_definitions = self._build_builtin_tab_definitions()
        self._refresh_navigation_tabs()
        self._refresh_classic_source_entries_if_active()

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
        dialog = AdvancedSettingsDialog(
            self.config,
            self._save_config,
            self,
            apply_theme=self._apply_application_theme,
            app_log_service=self._app_log_service,
            youtube_category_text_loader=self._youtube_category_text_loader,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if self.player_window is not None and hasattr(self.player_window, "refresh_runtime_video_output_settings"):
            self.player_window.refresh_runtime_video_output_settings()
        if self.youtube_page is not None:
            self.youtube_page.reload_categories()
        self.apply_home_mode(getattr(self.config, "home_mode", "browse") or "browse")

    def _open_media_folder(self, page: PosterGridPage, controller: Any, item: Any) -> None:
        page.invalidate_pending_item_requests()
        folder_id = str(getattr(item, "vod_id", "") or "")
        self._start_media_load(
            page,
            lambda: self._load_media_folder_page(controller, folder_id, 1),
            empty_message="当前文件夹暂无内容",
            push_breadcrumb=(folder_id, item.vod_name),
            folder_id=folder_id,
            folder_page_loader=lambda current_folder_id, current_page: self._load_media_folder_page(
                controller,
                current_folder_id,
                current_page,
            ),
        )

    def _handle_media_breadcrumb_requested(
        self,
        page: PosterGridPage,
        controller: Any,
        node_id: str,
        kind: str,
        index: int,
    ) -> None:
        page.invalidate_pending_item_requests()
        if kind == "folder":
            self._start_media_load(
                page,
                lambda: self._load_media_folder_page(controller, node_id, 1),
                empty_message="当前文件夹暂无内容",
                trim_breadcrumbs_to=index,
                folder_id=node_id,
                folder_page_loader=lambda current_folder_id, current_page: self._load_media_folder_page(
                    controller,
                    current_folder_id,
                    current_page,
                ),
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

    def _load_media_folder_page(self, controller: Any, folder_id: str, page: int):
        load_folder_items = getattr(controller, "load_folder_items")
        try:
            signature = inspect.signature(load_folder_items)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            parameters = list(signature.parameters.values())
            accepts_page = (
                any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters)
                or len(parameters) >= 2
            )
            if accepts_page:
                return load_folder_items(folder_id, page)
        if page <= 1:
            return load_folder_items(folder_id)
        load_items = getattr(controller, "load_items", None)
        if callable(load_items):
            try:
                return load_items(folder_id, page, filters={})
            except TypeError:
                return load_items(folder_id, page)
        return load_folder_items(folder_id)

    def _find_plugin_controller(self, plugin_id: int):
        for definition in self._plugin_definitions:
            if _plugin_value(definition, "id") == plugin_id:
                return _plugin_value(definition, "controller")
        return None

    def _source_display_name(self, source_kind: str, source_key: str = "") -> str:
        """Resolve the user-visible source label, honoring tab/plugin renames."""
        if source_kind in {"plugin", "spider_plugin"} and source_key:
            title = next(
                (
                    definition.title
                    for definition in self._plugin_tab_definitions
                    if definition.key == f"plugin:{source_key}"
                ),
                "",
            )
            if title:
                return title
        else:
            title = next(
                (
                    definition.title
                    for definition in self._builtin_tab_definitions
                    if definition.key == source_kind
                ),
                "",
            )
            if title:
                return title
        return self._favorite_source_name(source_kind, source_key)

    def _favorite_source_name(self, source_kind: str, source_key: str = "") -> str:
        if source_kind in {"plugin", "spider_plugin"} and source_key:
            title = next(
                (
                    definition.title
                    for definition in self._plugin_tab_definitions
                    if definition.key == f"plugin:{source_key}"
                ),
                "",
            )
            if title:
                return title
        return {
            "browse": "文件浏览",
            "plugin": "插件",
            "spider_plugin": "插件",
            "telegram": "电报影视",
            "telegram_channel": "电报频道",
            "bilibili": "B站",
            "youtube": "YouTube",
            "live": "网络直播",
            "emby": "Emby",
            "jellyfin": "Jellyfin",
            "feiniu": "飞牛影视",
            "direct_parse": "全局解析",
        }.get(source_kind, source_kind or "未知来源")

    def _favorite_record_source_label(self, record: FavoriteRecord) -> str:
        return self._favorite_source_name(record.source_kind, record.source_key)

    def _history_record_favorite_source(self, record: HistoryRecord) -> tuple[str, str, str]:
        if record.source_kind == "spider_plugin":
            source_key = record.source_key or str(record.source_plugin_id or "")
            source_name = self._favorite_source_name("plugin", source_key)
            if source_name == "插件":
                source_name = record.source_name or record.source_plugin_name or source_name
            return "plugin", source_key, source_name
        if record.source_kind == "remote":
            return "browse", "", "文件浏览"
        source_key = record.source_key or ""
        return record.source_kind or "browse", source_key, self._favorite_source_name(record.source_kind or "browse", source_key)

    def _history_record_favorite_payload(self, record: HistoryRecord) -> dict[str, object] | None:
        vod_id = str(record.key or "").strip()
        if not vod_id:
            return None
        source_kind, source_key, source_name = self._history_record_favorite_source(record)
        title = str(record.vod_name or "").strip()
        now = int(time.time())
        return {
            "source_kind": source_kind,
            "source_key": source_key,
            "source_name": source_name,
            "vod_id": vod_id,
            "vod_name_snapshot": title,
            "latest_vod_name": title,
            "vod_pic": str(record.vod_pic or ""),
            "vod_remarks": str(record.vod_remarks or ""),
            "title_changed": False,
            "created_at": now,
            "updated_at": now,
        }

    def _handle_history_context_favorite(self, record: HistoryRecord) -> None:
        payload = self._history_record_favorite_payload(record)
        if payload is None:
            return
        self._favorites_controller.add_favorite(payload)

    def _favorite_record_for_identity(self, source_kind: str, source_key: str, vod_id: str) -> FavoriteRecord:
        return FavoriteRecord(
            source_kind=source_kind,
            source_key=source_key,
            source_name=self._favorite_source_name(source_kind, source_key),
            vod_id=vod_id,
            vod_name_snapshot="",
            latest_vod_name="",
            vod_pic="",
            vod_remarks="",
            title_changed=False,
            created_at=0,
            updated_at=0,
        )

    def _toggle_browse_favorite(self, item: VodItem) -> None:
        if self._favorites_controller.is_favorited(source_kind="browse", source_key="", vod_id=item.vod_id):
            self._favorites_controller.remove_favorite([self._favorite_record_for_identity("browse", "", item.vod_id)])
            return
        now = int(time.time())
        self._favorites_controller.add_favorite(
            {
                "source_kind": "browse",
                "source_key": "",
                "source_name": "文件浏览",
                "vod_id": item.vod_id,
                "vod_name_snapshot": item.vod_name,
                "latest_vod_name": item.vod_name,
                "vod_pic": item.vod_pic,
                "vod_remarks": item.vod_remarks,
                "title_changed": False,
                "created_at": now,
                "updated_at": now,
            }
        )
        self._record_heat_media_event(
            "favorite_add",
            heat_identity_from_vod(item),
            context={"source_kind": "browse", "source_key": ""},
        )

    def _current_player_favorite_payload(self, item: PlayItem) -> dict[str, object] | None:
        if self.player_window is None or self.player_window.session is None:
            return None
        session = self.player_window.session
        vod = session.vod
        vod_id = str(item.vod_id or vod.vod_id or "").strip()
        if not vod_id:
            return None
        now = int(time.time())
        title = str(vod.vod_name or item.title or "").strip()
        return {
            **self._favorite_tmdb_identity_payload(vod, item),
            "source_kind": session.source_kind or "browse",
            "source_key": session.source_key or "",
            "source_name": self._favorite_source_name(session.source_kind or "browse", session.source_key or ""),
            "vod_id": vod_id,
            "vod_name_snapshot": title,
            "latest_vod_name": title,
            "vod_pic": str(vod.vod_pic or ""),
            "vod_remarks": str(vod.vod_remarks or ""),
            "title_changed": False,
            "created_at": now,
            "updated_at": now,
        }

    def _player_item_is_favorited(self, item: PlayItem) -> bool:
        payload = self._current_player_favorite_payload(item)
        if payload is None:
            return False
        return self._favorites_controller.is_favorited(
            source_kind=str(payload["source_kind"]),
            source_key=str(payload["source_key"]),
            vod_id=str(payload["vod_id"]),
        )

    def _toggle_player_item_favorite(self, item: PlayItem) -> None:
        payload = self._current_player_favorite_payload(item)
        if payload is None:
            return
        source_kind = str(payload["source_kind"])
        source_key = str(payload["source_key"])
        vod_id = str(payload["vod_id"])
        if self._favorites_controller.is_favorited(source_kind=source_kind, source_key=source_key, vod_id=vod_id):
            self._favorites_controller.remove_favorite([self._favorite_record_for_identity(source_kind, source_key, vod_id)])
            return
        self._favorites_controller.add_favorite(payload)
        session = self.player_window.session if self.player_window is not None else None
        self._record_heat_media_event(
            "favorite_add",
            heat_identity_from_vod(session.vod, item) if session is not None else None,
            context={"source_kind": source_kind, "source_key": source_key},
        )

    def _player_following_identity(self, item: PlayItem) -> tuple[str, str, str] | None:
        if self.player_window is None or self.player_window.session is None:
            return None
        session = self.player_window.session
        vod = session.vod
        source_kind = session.source_kind or "browse"
        source_key = session.source_key or ""
        identity = str(vod.vod_id or item.vod_id or item.media_title or item.title or "").strip()
        if not identity:
            return None
        return source_kind, source_key, f"{source_kind}:{source_key}:{identity}"

    def _player_following_external_ids(self, item: PlayItem) -> dict[str, str]:
        if self.player_window is None or self.player_window.session is None:
            return {}
        vod = self.player_window.session.vod
        external_ids = {"douban": str(vod.dbid)} if int(vod.dbid or 0) else {}
        for field in [*list(vod.detail_fields or []), *list(item.detail_fields or [])]:
            label = str(getattr(field, "label", "") or "").strip().lower()
            value = str(getattr(field, "value", "") or "").strip()
            if not value:
                continue
            if "tmdb" in label:
                external_ids["tmdb"] = value
            elif "bangumi" in label:
                external_ids["bangumi"] = value
            elif "豆瓣" in label or "douban" in label:
                external_ids["douban"] = value
        return external_ids

    def _player_following_matches_record(self, item: PlayItem, record) -> bool:
        identity = self._player_following_identity(item)
        if identity is None:
            return False
        source_kind, source_key, provider_id = identity
        if getattr(record, "provider", "") == "player" and getattr(record, "provider_id", "") == provider_id:
            return True
        target_vod_id = provider_id.split(":", 2)[-1]
        for binding in list(getattr(record, "source_bindings", []) or []):
            if (
                getattr(binding, "source_kind", "") == source_kind
                and getattr(binding, "source_key", "") == source_key
                and getattr(binding, "vod_id", "") == target_vod_id
            ):
                return True
        current_external_ids = self._player_following_external_ids(item)
        record_external_ids = {
            str(key): str(value)
            for key, value in dict(getattr(record, "external_ids", {}) or {}).items()
            if str(value or "").strip()
        }
        return any(
            record_external_ids.get(key) == value
            for key, value in current_external_ids.items()
            if value
        )

    def _player_following_record(self, item: PlayItem):
        if self._player_following_identity(item) is None or not hasattr(self._following_controller, "load_page"):
            return None
        records, _total = self._following_controller.load_page(
            page=1,
            size=1000,
            keyword="",
            only_updates=False,
        )
        for entry in records:
            record = getattr(entry, "record", entry)
            if self._player_following_matches_record(item, record):
                return record
        return None

    def _player_item_is_followed(self, item: PlayItem) -> bool:
        return self._player_following_record(item) is not None

    def _toggle_player_item_following(self, item: PlayItem) -> None:
        record = self._player_following_record(item)
        if record is not None and hasattr(self._following_controller, "delete"):
            self._following_controller.delete(record.id)
            self.following_page.load_page()
            return
        if (
            self.player_window is None
            or self.player_window.session is None
            or not hasattr(self._following_controller, "add_from_player")
        ):
            return
        source_kind = self.player_window.session.source_kind or "browse"
        source_key = self.player_window.session.source_key or ""
        position_seconds = 0
        video = getattr(self.player_window, "video", None)
        if video is not None and hasattr(video, "position_seconds"):
            try:
                position_seconds = int(video.position_seconds() or 0)
            except Exception:
                position_seconds = 0
        mark_current_episode = self._player_window_reached_playing()
        self._following_controller.add_from_player(
            vod=self.player_window.session.vod,
            item=item,
            source_kind=source_kind,
            source_key=source_key,
            position_seconds=position_seconds,
            playlist=list(getattr(self.player_window.session, "playlist", []) or []),
            mark_current_episode=mark_current_episode,
        )
        self.following_page.load_page()
        media = heat_identity_from_vod(self.player_window.session.vod, item)
        if has_required_heat_external_id(media):
            self._record_heat_media_event(
                "following_add",
                media,
                context={"source_kind": source_kind, "source_key": source_key},
            )

    def _player_window_reached_playing(self) -> bool:
        if self.player_window is None:
            return False
        startup_state = getattr(self.player_window, "_startup_state", None)
        stage = getattr(startup_state, "stage", None)
        if stage is None:
            return True
        return stage is PlaybackStartupStage.PLAYING or str(stage) == str(PlaybackStartupStage.PLAYING)

    def _report_player_item_following_progress(
        self,
        item: PlayItem,
        *,
        position_seconds: int,
        duration_seconds: int = 0,
    ) -> None:
        record = self._player_following_record(item)
        if (
            record is None
            or self.player_window is None
            or self.player_window.session is None
            or not hasattr(self._following_controller, "record_playback_progress")
        ):
            return
        decision = resolve_following_playback_progress(
            item,
            list(getattr(self.player_window.session, "playlist", []) or []),
            current_index=max(0, int(getattr(self.player_window, "current_index", 0) or 0)),
            fallback_season_number=int(
                getattr(record, "current_season_number", 0) or getattr(record, "season_number", 0) or 1
            ),
            position_seconds=position_seconds,
            duration_seconds=duration_seconds,
        )
        if decision is None or not decision.threshold_reached:
            return
        self._following_controller.record_playback_progress(
            record.id,
            current_season_number=decision.season_number,
            current_episode=decision.episode_number,
            position_seconds=position_seconds,
        )
        if hasattr(self._following_controller, "record_playback_source"):
            playlist = list(getattr(self.player_window.session, "playlist", []) or [])
            playlist_latest_episode = len(playlist) if len(playlist) > 1 else decision.episode_number
            self._following_controller.record_playback_source(
                record.id,
                source_kind=str(getattr(self.player_window.session, "source_kind", "") or "browse"),
                source_key=str(getattr(self.player_window.session, "source_key", "") or ""),
                vod_id=str(getattr(self.player_window.session.vod, "vod_id", "") or getattr(item, "vod_id", "") or ""),
                current_season_number=decision.season_number,
                current_episode=decision.episode_number,
                playlist_latest_episode=playlist_latest_episode,
            )

    def _current_player_episode_number(self) -> int:
        if self.player_window is None or self.player_window.session is None:
            return 0
        return max(0, int(getattr(self.player_window, "current_index", 0) or 0) + 1)

    def open_media_detail_from_vod(self, vod: VodItem) -> None:
        controller = self._media_detail_controller
        if controller is None or not hasattr(controller, "load_from_vod"):
            self.show_error("未配置媒体详情数据源")
            return
        placeholder = self._media_detail_placeholder("placeholder_from_vod", vod)
        self._open_media_detail_async(
            placeholder,
            lambda: controller.load_from_vod(vod),
        )

    def open_media_detail_from_heat(self, item: object) -> None:
        controller = self._media_detail_controller
        if controller is None or not hasattr(controller, "load_from_heat"):
            title = str(getattr(item, "title", "") or "").strip()
            if title:
                self.global_search_edit.setText(title)
                self._start_global_search(heat_source_kind="heat_recommendation")
            return
        placeholder = self._media_detail_placeholder("placeholder_from_heat", item)
        self._open_media_detail_async(
            placeholder,
            lambda: controller.load_from_heat(item),
        )

    def open_media_detail_from_identity(self, identity: MediaDetailIdentity) -> None:
        controller = self._media_detail_controller
        if controller is None or not hasattr(controller, "load_from_identity"):
            title = str(getattr(identity, "title", "") or "").strip()
            if title:
                self._handle_favorite_global_search(title)
            return
        placeholder = self._media_detail_placeholder("placeholder_from_identity", identity)
        self._open_media_detail_async(
            placeholder,
            lambda: controller.load_from_identity(identity),
        )

    def open_media_detail_from_related_request(self, payload) -> None:
        if isinstance(payload, MediaDetailIdentity):
            self.open_media_detail_from_identity(payload)
            return
        if isinstance(payload, MediaDetailLookup):
            self.open_media_detail_from_lookup(payload)
            return
        title = str(getattr(payload, "title", "") or "").strip()
        if title:
            self.open_media_detail_from_lookup(MediaDetailLookup(title=title))

    def open_media_detail_from_lookup(self, lookup: MediaDetailLookup) -> None:
        controller = self._media_detail_controller
        if controller is None or not hasattr(controller, "load_from_lookup"):
            title = str(getattr(lookup, "title", "") or "").strip()
            if title:
                self._handle_favorite_global_search(title)
            return
        placeholder = self._media_detail_placeholder("placeholder_from_lookup", lookup)
        self._open_media_detail_async(
            placeholder,
            lambda: controller.load_from_lookup(lookup),
        )

    def _media_detail_placeholder(self, method_name: str, payload):
        controller = self._media_detail_controller
        method = getattr(controller, method_name, None)
        if callable(method):
            try:
                return method(payload)
            except Exception:
                pass
        title = str(getattr(payload, "vod_name", "") or getattr(payload, "title", "") or "").strip()
        media_type = str(getattr(payload, "media_type", "") or getattr(payload, "vod_tag", "") or "tv").strip()
        identity = payload if isinstance(payload, MediaDetailIdentity) else MediaDetailIdentity(
            media_type=media_type or "tv",
            tmdb_id="",
            title=title,
        )
        from atv_player.controllers.media_detail_controller import MediaDetailView

        return MediaDetailView(
            identity=identity,
            title=str(getattr(identity, "title", "") or title or "媒体详情"),
            media_type=str(getattr(identity, "media_type", "") or media_type or "tv"),
            overview="正在加载元数据...",
        )

    def _open_media_detail_async(self, placeholder, loader) -> None:
        self._media_detail_load_request_id += 1
        request_id = self._media_detail_load_request_id
        self._show_media_detail_view(placeholder)
        self.media_detail_page.set_status("正在加载元数据...")

        def load() -> None:
            try:
                view = loader()
            except Exception as exc:
                if self._is_window_alive():
                    self._media_detail_load_signals.failed.emit(request_id, str(exc))
                return
            if self._is_window_alive():
                self._media_detail_load_signals.succeeded.emit(request_id, view)

        threading.Thread(target=load, daemon=True).start()

    def _handle_media_detail_load_succeeded(self, request_id: int, view) -> None:
        if request_id != self._media_detail_load_request_id:
            return
        self.media_detail_page.set_refresh_metadata_busy(False)
        self.media_detail_page.load_view(view)

    def _handle_media_detail_load_failed(self, request_id: int, message: str) -> None:
        if request_id != self._media_detail_load_request_id:
            return
        self.media_detail_page.set_refresh_metadata_busy(False)
        self.media_detail_page.set_status(f"元数据加载失败: {message}")

    def _show_media_detail_view(self, view) -> None:
        self.nav_tabs.ensure_widget(self.media_detail_page)
        self.nav_tabs.setNavigationVisible(True)
        self.nav_tabs.setVisible(True)
        self._home_stack.setCurrentWidget(self.nav_tabs)
        self.media_detail_page.load_view(view)
        self.nav_tabs.setCurrentWidget(self.media_detail_page)
        self._hide_global_search_popup()
        if self._heat_recommendations_dialog is not None:
            self._heat_recommendations_dialog.hide()

    def _return_from_media_detail(self) -> None:
        if self.global_catalog_page is not None and self.nav_tabs.indexOf(self.global_catalog_page) >= 0:
            self.nav_tabs.setCurrentWidget(self.global_catalog_page)
            return
        self._select_first_visible_tab()

    def search_play_for_media_detail(self, view) -> None:
        controller = self._media_detail_controller
        if controller is not None and hasattr(controller, "search_title"):
            keyword = str(controller.search_title(view) or "").strip()
        else:
            keyword = str(getattr(view, "title", "") or "").strip()
        if not keyword:
            return
        self.global_search_edit.setText(keyword)
        self.nav_tabs.setCurrentWidget(self.douban_page)
        self._start_global_search(include_smart_search=False)

    def add_following_from_media_detail(self, view) -> None:
        controller = self._media_detail_controller
        if controller is None or not hasattr(controller, "candidate_for_following"):
            self.search_play_for_media_detail(view)
            return
        try:
            candidate = controller.candidate_for_following(view)
            add_candidate = getattr(self._following_controller, "add_candidate", None)
            if not callable(add_candidate):
                raise RuntimeError("追更控制器不可用")
            add_candidate(candidate)
            self.following_page.load_page()
            self.media_detail_page.set_status("已加入追更")
        except Exception:
            self.media_detail_page.set_status("无法直接加入追更，已切换为搜索播放")
            self.search_play_for_media_detail(view)

    def refresh_media_detail_metadata(self, view) -> None:
        controller = self._media_detail_controller
        if controller is None or not hasattr(controller, "refresh"):
            self.media_detail_page.set_status("未配置媒体详情数据源")
            return
        self._media_detail_load_request_id += 1
        request_id = self._media_detail_load_request_id
        self.media_detail_page.set_status("正在更新元数据...")
        self.media_detail_page.set_refresh_metadata_busy(True)

        def refresh() -> None:
            try:
                refreshed = controller.refresh(view)
            except Exception as exc:
                if self._is_window_alive():
                    self._media_detail_load_signals.failed.emit(request_id, str(exc))
                return
            if self._is_window_alive():
                self._media_detail_load_signals.succeeded.emit(request_id, refreshed)

        threading.Thread(target=refresh, daemon=True).start()

    def load_media_detail_season(self, view, season_number: int) -> None:
        controller = self._media_detail_controller
        load_season = getattr(controller, "load_season", None)
        normalized_season = max(0, int(season_number or 0))
        if normalized_season <= 0:
            return
        if not callable(load_season):
            self.media_detail_page.set_status("当前详情数据源不支持分季加载")
            return
        self._media_detail_season_request_id += 1
        request_id = self._media_detail_season_request_id
        self.media_detail_page.set_status(f"正在加载第 {normalized_season} 季分集...")

        def load() -> None:
            try:
                refreshed = load_season(view, season_number=normalized_season)
            except Exception as exc:
                if self._is_window_alive():
                    self._media_detail_season_signals.failed.emit(request_id, normalized_season, str(exc))
                return
            if self._is_window_alive():
                self._media_detail_season_signals.succeeded.emit(
                    request_id,
                    refreshed,
                    normalized_season,
                )

        threading.Thread(target=load, daemon=True).start()

    def _handle_media_detail_season_succeeded(self, request_id: int, view, season_number: int) -> None:
        if request_id != self._media_detail_season_request_id:
            return
        self.media_detail_page.load_season_view(view, season_number=season_number)

    def _handle_media_detail_season_failed(self, request_id: int, season_number: int, message: str) -> None:
        if request_id != self._media_detail_season_request_id:
            return
        self.media_detail_page.set_status(f"第 {season_number} 季分集加载失败: {message}")

    def open_following_detail(self, following_id: int) -> None:
        following_id = int(following_id)
        self._following_controller.clear_homepage_prompt(following_id)
        self.nav_tabs.setNavigationVisible(True)
        self.nav_tabs.setVisible(True)
        self._home_stack.setCurrentWidget(self.nav_tabs)
        self.nav_tabs.setCurrentWidget(self.following_detail_page)
        self._close_following_prompt_dialog(already_handled=True)
        QTimer.singleShot(0, lambda: self.following_detail_page.load_record(following_id))

    def _return_to_following_page(self) -> None:
        self.following_page.load_page()
        self.nav_tabs.setCurrentWidget(self.following_page)

    def _unfollow_from_detail(self, following_id: int) -> None:
        self._following_controller.delete(following_id)
        self.following_page.load_page()
        self.nav_tabs.setCurrentWidget(self.following_page)

    def search_play_for_following(self, following_id: int) -> None:
        view = self._following_controller.load_detail(following_id)
        self._following_controller.clear_homepage_prompt(following_id)
        self.global_search_edit.setText(view.record.title)
        self.nav_tabs.setCurrentWidget(self.douban_page)
        self._close_following_prompt_dialog(already_handled=True)
        self._start_global_search(include_smart_search=False)

    def open_following_bound_source(self, following_id: int) -> None:
        view = self._following_controller.load_detail(int(following_id))
        binding = self._first_playable_following_binding(view.record)
        if binding is None:
            self.show_error("暂无已绑定播放源，请先搜索播放")
            return
        self._following_controller.clear_homepage_prompt(int(following_id))
        self._close_following_prompt_dialog(already_handled=True)
        record = getattr(view, "record", None)
        self._start_open_request(lambda: self._build_following_bound_source_request(binding, record=record))

    def _first_playable_following_binding(self, record):
        for binding in list(getattr(record, "source_bindings", []) or []):
            source_kind = str(getattr(binding, "source_kind", "") or "").strip()
            vod_id = str(getattr(binding, "vod_id", "") or "").strip()
            if source_kind and vod_id:
                return binding
        return None

    def _build_following_bound_source_request(self, binding, record=None):
        source_kind = str(getattr(binding, "source_kind", "") or "").strip()
        source_key = str(getattr(binding, "source_key", "") or "").strip()
        vod_id = str(getattr(binding, "vod_id", "") or "").strip()
        if source_kind == "browse":
            return self.browse_controller.build_request_from_detail(vod_id)
        if source_kind == "msub":
            if self.msub_controller is None:
                raise RuntimeError("服务端追剧不可用")
            # 继续播放语义:从上次看到的下一集开始(本地无进度时由服务端观看记录兜底)。
            start_episode = 0
            current_episode = int(getattr(record, "current_episode", 0) or 0)
            if current_episode > 0:
                start_episode = current_episode + 1
            return self.msub_controller.build_request(vod_id, start_episode=start_episode)
        if source_kind in {"plugin", "spider_plugin"}:
            controller = self._plugin_controller_by_id(source_key)
            if controller is None:
                raise RuntimeError("已绑定插件不可用")
            request = controller.build_request(vod_id)
            request.source_kind = "plugin"
            request.source_key = source_key
            return request
        controller_map = {
            "telegram": self.telegram_controller,
            "bilibili": self.bilibili_controller,
            "youtube": self.youtube_controller,
            "emby": self.emby_controller,
            "jellyfin": self.jellyfin_controller,
            "feiniu": self.feiniu_controller,
        }
        controller = controller_map.get(source_kind)
        if controller is None:
            raise RuntimeError("已绑定播放源不可用")
        return self._apply_request_playback_history_title(controller.build_request(vod_id))

    def _snooze_following_prompt(self, following_id: int) -> None:
        self._following_controller.snooze_prompt(following_id)
        self._close_following_prompt_dialog(already_handled=True)

    def _dismiss_following_prompt_until_next_episode(self, following_id: int) -> None:
        self._following_controller.dismiss_prompt_until_next_episode(following_id)
        self._close_following_prompt_dialog(already_handled=True)

    def _dismiss_following_prompt(self, following_id: int) -> None:
        if not self._following_prompt_close_handled:
            self._following_controller.clear_homepage_prompt(following_id)
        self._following_prompt_close_handled = False
        self._following_prompt_dialog = None

    def _close_following_prompt_dialog(self, *, already_handled: bool = False) -> None:
        if self._following_prompt_dialog is not None:
            self._following_prompt_close_handled = already_handled
            self._following_prompt_dialog.close()
        self._following_prompt_dialog = None

    def _player_window_is_playing(self) -> bool:
        if self.player_window is None:
            return False
        if bool(getattr(self.player_window, "is_playing", False)):
            return True
        is_visible = getattr(self.player_window, "isVisible", None)
        if callable(is_visible):
            try:
                return bool(is_visible())
            except RuntimeError:
                return False
        return False

    def show_following_homepage_prompts(self) -> None:
        if self._player_window_is_playing():
            return
        if self._following_prompt_dialog is not None:
            return
        records = list(self._following_controller.load_homepage_prompts())
        if not records:
            return
        record = records[0]
        self._following_prompt_close_handled = False
        dialog = ThemedDialogBase(title="追更更新", parent=self, resizable=False)
        layout = dialog.content_layout()
        title_label = QLabel(record.title, dialog)
        detail_label = QLabel(
            f"更新 {record.new_episode_count} 集，最新第 {record.latest_episode} 集",
            dialog,
        )
        if record.new_episode_count > 0:
            try:
                prompt_view = self._following_controller.load_detail(record.id, refresh_if_empty=False)
                prompt_snapshot = prompt_view.snapshot
            except Exception:
                prompt_snapshot = None
            if prompt_snapshot is not None and (prompt_snapshot.seasons or prompt_snapshot.episodes):
                from atv_player.following_models import resolve_new_episode_count as _resolve_count, resolve_progress_season as _resolve_season
                prompt_current_season = _resolve_season(
                    record.current_season_number, record.current_episode, fallback_season=record.season_number,
                )
                snapshot_seasons = [int(s.season_number) for s in prompt_snapshot.seasons if int(s.season_number or 0) > 0]
                snapshot_seasons.extend(int(e.season_number) for e in prompt_snapshot.episodes if int(e.season_number or 0) > 0)
                prompt_latest_season = max(max(snapshot_seasons, default=0), prompt_current_season)
                absolute_count = _resolve_count(
                    has_update=True,
                    current_season_number=prompt_current_season,
                    current_episode=record.current_episode,
                    latest_season_number=prompt_latest_season,
                    latest_episode=record.latest_episode,
                    total_episodes=record.total_episodes,
                    seasons=prompt_snapshot.seasons,
                    episodes=prompt_snapshot.episodes,
                )
                detail_label.setText(f"更新 {absolute_count} 集，最新第 {record.latest_episode} 集")
        button_row = QHBoxLayout()
        self._following_prompt_detail_button = QPushButton("查看详情", dialog)
        self._following_prompt_search_button = QPushButton("搜索播放", dialog)
        self._following_prompt_snooze_button = QPushButton("稍后提醒", dialog)
        self._following_prompt_dismiss_until_next_button = QPushButton("不再提示", dialog)
        button_row.addWidget(self._following_prompt_detail_button)
        button_row.addWidget(self._following_prompt_search_button)
        button_row.addWidget(self._following_prompt_snooze_button)
        button_row.addWidget(self._following_prompt_dismiss_until_next_button)
        layout.addWidget(title_label)
        layout.addWidget(detail_label)
        layout.addLayout(button_row)
        self._following_prompt_detail_button.clicked.connect(
            lambda: self.open_following_detail(record.id)
        )
        self._following_prompt_search_button.clicked.connect(
            lambda: self.search_play_for_following(record.id)
        )
        self._following_prompt_snooze_button.clicked.connect(
            lambda: self._snooze_following_prompt(record.id)
        )
        self._following_prompt_dismiss_until_next_button.clicked.connect(
            lambda: self._dismiss_following_prompt_until_next_episode(record.id)
        )

        def handle_finished(_result, following_id=record.id):
            self._dismiss_following_prompt(following_id)

        dialog.finished.connect(handle_finished)
        self._following_prompt_dialog = dialog
        dialog.show()

    def open_history_detail(self, record: HistoryRecord) -> None:
        if record.source_kind == "direct_parse":
            self._start_open_request(lambda: self._build_parse_request(record.key))
            return
        if record.source_kind == "msub":
            if self.msub_controller is None:
                self.show_error("服务端追剧不可用")
                return

            def build_request() -> OpenPlayerRequest:
                request = self.msub_controller.build_request(record.key)
                return self._apply_request_playback_history_title(request)

            self._start_open_request(build_request)
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
            self._start_open_request(lambda: self._apply_request_playback_history_title(self.emby_controller.build_request(record.key)))
            return
        if record.source_kind == "telegram":
            self._start_open_request(lambda: self._apply_request_playback_history_title(self.telegram_controller.build_request(record.key)))
            return
        if record.source_kind == "telegram_channel":
            self._start_open_request(
                lambda: self._apply_request_playback_history_title(self.telegram_channel_controller.build_request(record.key))
            )
            return
        if record.source_kind == "bilibili":
            self._start_open_request(
                lambda: self._apply_request_playback_history_title(self.bilibili_controller.build_request(record.key))
            )
            return
        if record.source_kind == "youtube":
            self._start_open_request(
                lambda: self._apply_request_playback_history_title(self.youtube_controller.build_request(record.key))
            )
            return
        if record.source_kind == "jellyfin":
            self._start_open_request(
                lambda: self._apply_request_playback_history_title(self.jellyfin_controller.build_request(record.key))
            )
            return
        if record.source_kind == "feiniu":
            self._start_open_request(lambda: self._apply_request_playback_history_title(self.feiniu_controller.build_request(record.key)))
            return
        self._start_open_request(
            lambda: self.browse_controller.build_request_from_detail(
                record.key, source_key=record.source_key or "csp_AList"
            )
        )

    def open_favorite_detail(self, record: FavoriteRecord) -> None:
        if record.source_kind == "direct_parse":
            self._open_favorite_placeholder(record)
            self._start_open_request(lambda: self._build_parse_request(record.vod_id))
            return
        if record.source_kind in {"plugin", "spider_plugin"}:
            controller = self._plugin_controller_by_id(record.source_key)
            if controller is None:
                self.show_error(f"没有可播放的项目: {record.source_name or record.vod_id}")
                return
            self._open_favorite_placeholder(record)
            self._start_open_request(lambda: self._prepare_favorite_request(controller.build_request(record.vod_id), record))
            return
        self._open_favorite_placeholder(record)
        if record.source_kind == "telegram":
            self._start_open_request(lambda: self.telegram_controller.build_request(record.vod_id))
            return
        if record.source_kind == "telegram_channel":
            self._start_open_request(lambda: self.telegram_channel_controller.build_request(record.vod_id))
            return
        if record.source_kind == "bilibili":
            self._start_open_request(lambda: self.bilibili_controller.build_request(record.vod_id))
            return
        if record.source_kind == "youtube":
            self._start_open_request(lambda: self.youtube_controller.build_request(record.vod_id))
            return
        if record.source_kind == "live":
            self._start_open_request(lambda: self.live_controller.build_request(record.vod_id))
            return
        if record.source_kind == "emby":
            self._start_open_request(lambda: self.emby_controller.build_request(record.vod_id))
            return
        if record.source_kind == "jellyfin":
            self._start_open_request(lambda: self.jellyfin_controller.build_request(record.vod_id))
            return
        if record.source_kind == "feiniu":
            self._start_open_request(lambda: self.feiniu_controller.build_request(record.vod_id))
            return
        self._start_open_request(
            lambda: self.browse_controller.build_request_from_detail(
                record.vod_id, source_key=record.source_key or "csp_AList"
            )
        )

    def _open_favorite_placeholder(self, record: FavoriteRecord) -> None:
        source_kind = "plugin" if record.source_kind == "spider_plugin" else record.source_kind or "browse"
        placeholder = self._build_placeholder_player_request(
            VodItem(
                vod_id=record.vod_id,
                vod_name=record.latest_vod_name or record.vod_name_snapshot or record.vod_id,
                vod_pic=record.vod_pic,
                vod_remarks=record.vod_remarks,
            ),
            source_kind=source_kind,
            source_key=record.source_key,
        )
        self._open_player_immediately(placeholder)

    @staticmethod
    def _prepare_favorite_request(request: OpenPlayerRequest, record: FavoriteRecord) -> OpenPlayerRequest:
        if record.source_kind in {"plugin", "spider_plugin"}:
            request.source_kind = "plugin"
            request.source_key = record.source_key
        return request

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
        if request_id == self._youtube_open_request_id:
            self._youtube_open_request_id = 0
            self._youtube_open_request_vod_id = ""
        if request_id != self._open_request_id:
            return
        self.open_player(request)

    def _handle_open_request_failed(self, request_id: int, message: str) -> None:
        if request_id == self._youtube_open_request_id:
            self._youtube_open_request_id = 0
            self._youtube_open_request_vod_id = ""
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
        folder_id: str = "",
        folder_page_loader: Any | None = None,
        page_number: int = 1,
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
                        folder_id=folder_id,
                        folder_page_loader=folder_page_loader,
                        page_number=page_number,
                    ),
                )

        threading.Thread(target=run, daemon=True).start()
        return request_id

    def _handle_media_load_succeeded(self, request_id: int, result: _MediaLoadResult) -> None:
        if request_id != self._media_request_id:
            return
        if result.folder_id and result.folder_page_loader is not None:
            result.page.show_folder_items(
                result.items,
                result.total,
                folder_id=result.folder_id,
                page_loader=result.folder_page_loader,
                page=result.page_number,
                empty_message=result.empty_message,
            )
        else:
            result.page.clear_folder_view()
            result.page.show_items(result.items, result.total, page=result.page_number, empty_message=result.empty_message)
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
            source_kind=request.source_kind,
            source_key=request.source_key,
            source_display_name=request.source_display_name,
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
            subtitle_search_service=self._subtitle_search_service,
            metadata_binding_repository=request.metadata_binding_repository,
            episode_title_override_repository=request.episode_title_override_repository,
            episode_title_enhancer=request.episode_title_enhancer,
            danmaku_controller=request.danmaku_controller,
            playback_progress_reporter=request.playback_progress_reporter,
            playback_stopper=request.playback_stopper,
            playback_history_loader=request.playback_history_loader,
            playback_history_saver=request.playback_history_saver,
            initial_log_message=request.initial_log_message,
            is_placeholder=request.is_placeholder,
            drive_resource_id=request.drive_resource_id,
            drive_files_loader=request.drive_files_loader,
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
        # Resolve the user-visible source label (honors tab/plugin renames) for the player
        # window title. Done before the placeholder short-circuit so placeholders show it too.
        if not request.source_display_name and request.source_kind:
            request.source_display_name = self._source_display_name(request.source_kind, request.source_key)
        if request.is_placeholder:
            return request
        if (
            request.metadata_hydrator is None
            and self._metadata_hydrator_factory is not None
            and request.source_kind in {"browse", "telegram", "telegram_channel", "emby", "jellyfin", "feiniu", "bilibili"}
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
            and request.source_kind in {"browse", "telegram", "telegram_channel", "plugin", "emby", "jellyfin", "feiniu", "bilibili"}
        ):
            request.metadata_scrape_service = self._metadata_scrape_service_factory(
                request=request,
                source_kind=request.source_kind,
                source_key=request.source_key,
                vod=request.vod,
            )
        if (
            request.danmaku_controller is None
            and self._danmaku_controller_factory is not None
            and request.source_kind in {"browse", "telegram", "telegram_channel", "emby", "jellyfin", "feiniu", "msub"}
        ):
            request.danmaku_controller = self._danmaku_controller_factory(
                request=request,
                source_kind=request.source_kind,
                source_key=request.source_key,
                vod=request.vod,
            )
        if (
            request.episode_title_enhancer is None
            and self._episode_title_enhancer_factory is not None
            and request.source_kind in {"browse", "telegram", "telegram_channel", "emby", "jellyfin", "feiniu", "msub"}
        ):
            request.episode_title_enhancer = self._episode_title_enhancer_factory(
                request=request,
                source_kind=request.source_kind,
                source_key=request.source_key,
                vod=request.vod,
            )
        if request.metadata_binding_repository is None:
            request.metadata_binding_repository = self._metadata_binding_repository
        if request.episode_title_override_repository is None:
            request.episode_title_override_repository = self._episode_title_override_repository
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

    def _create_player_window(self):
        kwargs = {
            "app_log_service": self._app_log_service,
            "m3u8_ad_filter": self._m3u8_ad_filter,
            "playback_parser_service": self._playback_parser_service,
            "default_video_cover_loader": self._default_video_cover_loader,
            "favorite_is_active": self._player_item_is_favorited,
            "favorite_toggle": self._toggle_player_item_favorite,
            "following_is_active": self._player_item_is_followed,
            "following_toggle": self._toggle_player_item_following,
            "following_progress_reporter": self._report_player_item_following_progress,
            "heat_controller": self._heat_controller,
        }
        try:
            parameters = inspect.signature(PlayerWindow).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if not accepts_kwargs:
            kwargs = {
                key: value
                for key, value in kwargs.items()
                if key in parameters
            }
        return PlayerWindow(
            self.player_controller,
            self.config,
            self._save_config,
            **kwargs,
        )

    def _apply_open_player(self, request, session, restore_paused_state: bool = False) -> None:
        if self.player_window is None:
            self.player_window = self._create_player_window()
            setattr(
                self.player_window,
                "_on_window_closed",
                lambda current_window=self.player_window: self._handle_player_window_closed(current_window),
            )
            if hasattr(self.player_window, "closed_to_main"):
                self.player_window.closed_to_main.connect(self._show_main_again)
            if hasattr(self.player_window, "global_search_requested"):
                self.player_window.global_search_requested.connect(self._handle_favorite_global_search)
        self._close_help_dialog()
        self._close_following_prompt_dialog(already_handled=True)
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
        self.config.main_window_geometry = self._capture_main_window_geometry()
        self._save_config()
        self.player_window.open_session(session, start_paused=start_paused)
        self.player_window.show()
        self.player_window.raise_()
        self.player_window.activateWindow()
        QTimer.singleShot(0, self.hide)

    def _handle_player_window_closed(self, player_window: object) -> None:
        if self.player_window is player_window:
            self.player_window = None

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
        QTimer.singleShot(
            0,
            lambda request_id=request_id, request=request, session=session, restore_paused_state=restore_paused_state: (
                self._apply_open_player(request, session, restore_paused_state=restore_paused_state)
                if request_id == self._player_session_request_id and self._is_window_alive()
                else None
            ),
        )

    def _handle_session_open_failed(self, request_id: int, message: str, restore_paused_state: bool) -> None:
        if request_id != self._player_session_request_id:
            return
        if restore_paused_state:
            self._discard_restore_placeholder_and_return_to_main()
            self.config.last_active_window = "main"
            self._save_config()
        self.show_error(message)

    def _show_main_again(self) -> None:
        # close() 占位窗口会触发 closeEvent 再次 emit closed_to_main,重入时直接忽略。
        if self._returning_to_main:
            return
        self._returning_to_main = True
        try:
            if self._current_player_session_is_placeholder():
                # 返回主窗口即放弃恢复:作废在途恢复/开会话请求,否则迟到的后台
                # 恢复链路会重新弹出播放窗口并把用户拉离主窗口。
                self._restore_request_id += 1
                self._next_player_session_request_id()
                close_player = getattr(self.player_window, "close", None) if self.player_window is not None else None
                if callable(close_player):
                    close_player()
                self.player_window = None
            elif self.player_window is not None and getattr(self.player_window, "session", None) is None:
                self.player_window = None
            self.config.last_active_window = "main"
            self._save_config()
            self._restore_main_window_after_player()
            self._refresh_media_home_if_active()
            self.raise_()
            self.activateWindow()
        finally:
            self._returning_to_main = False

    def _remember_main_window_state_for_player(self) -> None:
        self._main_window_was_maximized_before_player = self.isMaximized()
        geometry = self.normalGeometry() if self._main_window_was_maximized_before_player else self.geometry()
        if geometry.isValid():
            self._main_window_geometry_before_player = QRect(geometry)
        else:
            self._main_window_geometry_before_player = QRect()

    def _restore_main_window_after_player(self) -> None:
        geometry = QRect(self._main_window_geometry_before_player)
        if geometry.isValid():
            self.setGeometry(geometry)
            if self._main_window_was_maximized_before_player:
                self.showMaximized()
                QTimer.singleShot(0, self.showMaximized)
            else:
                self.showNormal()
            self._refresh_main_window_layout()
            QTimer.singleShot(0, self._refresh_main_window_layout)
            return
        self._restore_saved_geometry()
        self.show()
        if self._main_window_was_maximized_before_player or self._saved_main_window_geometry_is_maximized():
            self.showMaximized()
            QTimer.singleShot(0, self.showMaximized)
        self._refresh_main_window_layout()
        QTimer.singleShot(0, self._refresh_main_window_layout)

    def _capture_main_window_geometry(self) -> bytes:
        rect = self._last_normal_geometry
        if not rect.isValid() or rect.width() <= 0 or rect.height() <= 0:
            rect = QRect(self.x(), self.y(), max(1, self.width()), max(1, self.height()))
        payload = {
            "x": rect.x(),
            "y": rect.y(),
            "width": rect.width(),
            "height": rect.height(),
            "maximized": bool(self.isMaximized()),
        }
        return _CUSTOM_MAIN_WINDOW_GEOMETRY_PREFIX + json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def _restore_saved_geometry(self, *, apply_maximized: bool = False) -> None:
        geometry = self.config.main_window_geometry
        if not geometry:
            return
        if geometry.startswith(_CUSTOM_MAIN_WINDOW_GEOMETRY_PREFIX):
            try:
                payload = json.loads(geometry[len(_CUSTOM_MAIN_WINDOW_GEOMETRY_PREFIX) :].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            x = int(payload.get("x", 0))
            y = int(payload.get("y", 0))
            width = max(1, int(payload.get("width", 0)))
            height = max(1, int(payload.get("height", 0)))
            if width > 0 and height > 0:
                fitted = _fit_rect_within_available_screens(
                    QRect(x, y, width, height),
                    _available_screen_geometries(),
                )
                self.setGeometry(fitted)
                self._last_normal_geometry = QRect(fitted)
            if apply_maximized and bool(payload.get("maximized", False)):
                self.showMaximized()
            return
        self.restoreGeometry(to_qbytearray(geometry))

    def _saved_main_window_geometry_is_maximized(self) -> bool:
        geometry = self.config.main_window_geometry
        if not geometry or not geometry.startswith(_CUSTOM_MAIN_WINDOW_GEOMETRY_PREFIX):
            return False
        try:
            payload = json.loads(geometry[len(_CUSTOM_MAIN_WINDOW_GEOMETRY_PREFIX) :].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return bool(payload.get("maximized", False))

    def _refresh_main_window_layout(self) -> None:
        central_widget = self.centralWidget()
        if central_widget is None:
            return
        central_widget.updateGeometry()
        layout = central_widget.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()

    def _update_last_normal_geometry(self) -> None:
        if self.isMaximized() or self.isFullScreen():
            return
        rect = QRect(self.x(), self.y(), self.width(), self.height())
        if rect.isValid() and rect.width() > 0 and rect.height() > 0:
            self._last_normal_geometry = rect

    def show_or_restore_player(self) -> PlayerWindow | None:
        if self.player_window is not None and getattr(self.player_window, "session", None) is not None:
            self._dismiss_visible_global_search_popup()
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
        request = self._prepare_request_for_open(request)
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
            request = self.telegram_controller.build_request(vod_id)
            return self._apply_request_playback_history_title(request)
        if source == "telegram_channel":
            request = self.telegram_channel_controller.build_request(vod_id)
            return self._apply_request_playback_history_title(request)
        if source == "live":
            return self.live_controller.build_request(vod_id)
        if source == "emby":
            request = self.emby_controller.build_request(vod_id)
            return self._apply_request_playback_history_title(request)
        if source == "bilibili":
            request = self.bilibili_controller.build_request(vod_id)
            return self._apply_request_playback_history_title(request)
        if source == "youtube":
            request = self.youtube_controller.build_request(vod_id)
            return self._apply_request_playback_history_title(request)
        if source == "jellyfin":
            request = self.jellyfin_controller.build_request(vod_id)
            return self._apply_request_playback_history_title(request)
        if source == "plugin":
            controller = self._plugin_controller_by_id(self.config.last_playback_source_key)
            if controller is None:
                raise ValueError("找不到已保存的插件来源")
            request = controller.build_request(vod_id)
            request.source_kind = "plugin"
            request.source_key = self.config.last_playback_source_key
            return self._apply_request_playback_history_title(request)
        request = self.browse_controller.build_request_from_detail(vod_id)
        return self._apply_request_playback_history_title(request)

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

    def _build_main_window_help_payload(self) -> tuple[list[SystemInfoEntry], str, str]:
        system_info_rows = list(collect_system_info_entries())
        lines = ["系统信息"]
        lines.extend(f"{entry.label}: {entry.value}" for entry in system_info_rows)
        return system_info_rows, "\n".join(lines), self._build_detailed_diagnostics_text(system_info_rows)

    def _build_detailed_diagnostics_text(self, system_info_rows: list[SystemInfoEntry]) -> str:
        sections = [
            self._build_detailed_system_info_section(system_info_rows),
            self._build_detailed_runtime_section(),
            self._build_detailed_config_section(),
            self._build_detailed_plugin_section(),
            self._build_detailed_log_section(),
        ]
        return "\n\n".join(section for section in sections if section.strip())

    def _build_detailed_system_info_section(self, system_info_rows: list[SystemInfoEntry]) -> str:
        lines = ["系统信息"]
        lines.extend(f"{entry.label}: {entry.value}" for entry in system_info_rows)
        return "\n".join(lines)

    def _build_detailed_runtime_section(self) -> str:
        executable_path = ""
        app = QApplication.instance()
        if app is not None:
            executable_path = app.applicationFilePath().strip()
        lines = ["运行环境"]
        lines.append(f"Qt 平台: {QGuiApplication.platformName() or '不可用'}")
        lines.append(f"CPU 架构: {platform.machine() or '不可用'}")
        lines.append(f"可执行文件: {executable_path or '不可用'}")
        lines.append(f"数据目录: {app_data_dir()}")
        lines.append(f"缓存目录: {app_cache_dir()}")
        return "\n".join(lines)

    def _build_detailed_config_section(self) -> str:
        lines = ["应用配置摘要"]
        lines.append(f"主题: {self.config.theme_mode}")
        lines.append(f"代理模式: {self.config.network_proxy_mode}")
        lines.append(f"后端地址: {self.config.base_url}")
        lines.append(f"最后活动窗口: {self.config.last_active_window}")
        lines.append(f"日志记录: {'开启' if self.config.logging_enabled else '关闭'}")
        lines.append(f"元数据增强: {'开启' if self.config.metadata_enhancement_enabled else '关闭'}")
        return "\n".join(lines)

    def _build_detailed_plugin_section(self) -> str:
        plugin_names = self._list_enabled_plugin_names()
        lines = ["插件摘要", f"已启用插件数: {len(plugin_names)}"]
        lines.extend(plugin_names or ["无"])
        return "\n".join(lines)

    def _list_enabled_plugin_names(self) -> list[str]:
        if self._plugin_manager is None:
            return []
        list_plugins = getattr(self._plugin_manager, "list_plugins", None)
        if not callable(list_plugins):
            return []
        names: list[str] = []
        for plugin in list_plugins() or []:
            if not bool(getattr(plugin, "enabled", False)):
                continue
            display_name = str(getattr(plugin, "display_name", "") or getattr(plugin, "name", "") or "").strip()
            if display_name:
                names.append(display_name)
        return names

    def _build_detailed_log_section(self) -> str:
        lines = ["最近日志"]
        lines.extend(self._load_recent_app_log_lines(limit=20) or ["无"])
        return "\n".join(lines)

    def _load_recent_app_log_lines(self, limit: int) -> list[str]:
        if self._app_log_service is None:
            return []
        load_records = getattr(self._app_log_service, "load_records", None)
        if not callable(load_records):
            return []
        try:
            records = load_records(limit=limit, log_filter=AppLogFilter())
        except Exception:
            return ["不可用"]
        lines: list[str] = []
        for record in records or []:
            try:
                parts = [
                    f"[{record.timestamp}]",
                    str(record.level),
                    f"{record.source}/{record.category}",
                    str(record.message),
                ]
            except Exception:
                continue
            lines.append(" ".join(parts))
        return lines

    def _show_shortcut_help(self) -> None:
        dialog = show_shortcut_help_dialog(
            self,
            context="main_window",
            existing_dialog=self.help_dialog,
            quit_sequence=self.quit_shortcut.key(),
            system_info_rows=[SystemInfoEntry("依赖状态", "检查中...")],
            diagnostics_text="系统信息\n依赖状态: 检查中...",
            detailed_diagnostics_text="系统信息\n依赖状态: 检查中...",
        )
        if dialog is self.help_dialog:
            return
        self.help_dialog = dialog
        dialog.destroyed.connect(self._clear_help_dialog_reference)
        self._request_help_payload()

    def _request_help_payload(self) -> None:
        self._help_payload_request_id += 1
        request_id = self._help_payload_request_id

        def run() -> None:
            system_info_rows, diagnostics_text, detailed_diagnostics_text = self._build_main_window_help_payload()
            if self._is_window_alive():
                self._help_payload_signals.loaded.emit(
                    request_id,
                    system_info_rows,
                    diagnostics_text,
                    detailed_diagnostics_text,
                )

        threading.Thread(target=run, daemon=True).start()

    def _handle_help_payload_loaded(
        self,
        request_id: int,
        system_info_rows: object,
        diagnostics_text: str,
        detailed_diagnostics_text: str,
    ) -> None:
        if request_id != self._help_payload_request_id:
            return
        dialog = self.help_dialog
        if dialog is None or not dialog.isVisible():
            return
        dialog.update_system_info(
            list(system_info_rows),
            diagnostics_text=diagnostics_text,
            detailed_diagnostics_text=detailed_diagnostics_text,
        )

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
        self.config.main_window_geometry = self._capture_main_window_geometry()
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
        self._update_last_normal_geometry()
        app = QApplication.instance()
        if app is not None and not self._app_event_filter_installed:
            app.installEventFilter(self)
            self._app_event_filter_installed = True
        if app is not None and not self._app_state_signal_connected:
            app.applicationStateChanged.connect(self._handle_application_state_changed)
            self._app_state_signal_connected = True
        self._refresh_navigation_tabs()
        if (getattr(self.config, "home_mode", "browse") or "browse") != "classic":
            self._start_startup_plugin_load()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_last_normal_geometry()
        self._refresh_navigation_tabs()
        if self._plugin_overflow_drawer.isVisible():
            self._position_plugin_overflow_drawer()
        self._position_global_search_popup()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._update_last_normal_geometry()
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
        self.config.main_window_geometry = self._capture_main_window_geometry()
        if self.isVisible():
            self.config.last_active_window = "main"
        self._save_config()
        super().closeEvent(event)
