from __future__ import annotations

import html
import json
import queue
import re
import threading
import time
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import httpx
from PySide6.QtCore import QEvent, QObject, QRect, QSize, QTimer, Qt, QUrl, QUrlQuery, Signal
from PySide6.QtGui import (
    QActionGroup,
    QCloseEvent,
    QColor,
    QContextMenuEvent,
    QCursor,
    QIcon,
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPixmap,
    QShortcut,
    QWindow,
)
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QStyleOptionSlider, QToolTip
from PySide6.QtWidgets import (
    QComboBox,
    QColorDialog,
    QDoubleSpinBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedLayout,
    QTabBar,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from atv_player.danmaku.cache import load_or_create_danmaku_ass_cache
from atv_player.models import (
    ExternalSubtitleOption,
    ExternalSubtitleSelection,
    PlayItem,
    PlaybackDetailAction,
    PlaybackDetailField,
    PlaybackDetailFieldAction,
    PlaybackLoadResult,
    VideoQualityOption,
    VodItem,
)
from atv_player.player.bluray_iso import is_remote_iso_url
from atv_player.player.m3u8_ad_filter import M3U8AdFilter
from atv_player.player.mpv_widget import AudioTrack, MpvWidget, SubtitleTrack
from atv_player.ui.async_guard import AsyncGuardMixin
from atv_player.ui.help_dialog import ShortcutHelpDialog, show_shortcut_help_dialog
from atv_player.ui.icon_cache import load_icon
from atv_player.ui.poster_loader import load_remote_poster_image, normalize_poster_url, poster_cache_path
from atv_player.ui.qt_compat import qbytearray_to_bytes, to_qbytearray

_DANMAKU_SEARCH_PROVIDER_OPTIONS: list[tuple[str, str]] = [
    ("", "全部"),
    ("tencent", "腾讯"),
    ("youku", "优酷"),
    ("bilibili", "B站"),
    ("iqiyi", "爱奇艺"),
    ("mgtv", "芒果"),
]

_INLINE_METADATA_CR_RE = re.compile(r"\[a=cr:(?P<payload>\{.*?\})/\](?P<label>.*?)\[/a\]", re.DOTALL)


class ClickableSlider(QSlider):
    """A QSlider that allows clicking on the groove to set position directly."""

    clicked_value = Signal(int)

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self._hover_tooltip_formatter: Callable[[int], str] | None = None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            handle_rect = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider,
                option,
                QStyle.SubControl.SC_SliderHandle,
                self,
            )

            if handle_rect.contains(event.position().toPoint()):
                super().mousePressEvent(event)
                return

            value = self._pixel_pos_to_value(int(event.position().x()))
            self.setValue(value)
            self.clicked_value.emit(value)
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._show_hover_tooltip(event)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        QToolTip.hideText()
        super().leaveEvent(event)

    def set_hover_tooltip_formatter(self, formatter: Callable[[int], str] | None) -> None:
        self._hover_tooltip_formatter = formatter
        self.setMouseTracking(formatter is not None)

    def _pixel_pos_to_value(self, pos: int) -> int:
        groove_rect = self.rect()
        handle_width = 12
        available_width = groove_rect.width() - handle_width

        if available_width <= 0:
            return self.minimum()

        adjusted_pos = pos - handle_width // 2
        adjusted_pos = max(0, min(adjusted_pos, available_width))

        value_range = self.maximum() - self.minimum()
        value = self.minimum() + int((adjusted_pos / available_width) * value_range)
        return value

    def _show_hover_tooltip(self, event: QMouseEvent) -> None:
        if self._hover_tooltip_formatter is None:
            return
        value = self._pixel_pos_to_value(int(event.position().x()))
        text = self._hover_tooltip_formatter(value)
        if text:
            QToolTip.showText(event.globalPosition().toPoint(), text, self)
        else:
            QToolTip.hideText()


class _PosterLoadSignals(QObject):
    loaded = Signal(int, object)


class PosterBackgroundWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._background_pixmap = QPixmap()

    def set_background_pixmap(self, pixmap: QPixmap) -> None:
        self._background_pixmap = QPixmap(pixmap) if not pixmap.isNull() else QPixmap()
        self.update()

    def clear_background_pixmap(self) -> None:
        self._background_pixmap = QPixmap()
        self.update()

    def background_pixmap(self) -> QPixmap:
        return QPixmap(self._background_pixmap)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = self.rect()
        try:
            if self._background_pixmap.isNull() or rect.width() <= 0 or rect.height() <= 0:
                painter.fillRect(rect, QColor("#f6f8fb"))
                return

            scaled = self._background_pixmap.scaled(
                rect.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            source_rect = QRect(
                max((scaled.width() - rect.width()) // 2, 0),
                max((scaled.height() - rect.height()) // 2, 0),
                rect.width(),
                rect.height(),
            )
            painter.drawPixmap(rect, scaled, source_rect)
            painter.fillRect(rect, QColor(246, 248, 251, 214))
        finally:
            painter.end()


class _PlayItemResolveSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)


class _BackgroundTaskSignals(QObject):
    failed = Signal(str)


class _DanmakuSourceTaskSignals(QObject):
    finished = Signal(object, bool)


class _PlaybackPrepareSignals(QObject):
    succeeded = Signal(int, str)
    failed = Signal(int, str)


class _PlaybackLoaderSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)


class _DetailActionSignals(QObject):
    succeeded = Signal(int, object, object)
    failed = Signal(int, str)


@dataclass(slots=True)
class SubtitlePreference:
    mode: str = "auto"
    title: str = ""
    lang: str = ""
    is_default: bool = False
    is_forced: bool = False


@dataclass(slots=True)
class SecondarySubtitlePreference:
    mode: str = "off"
    title: str = ""
    lang: str = ""
    is_default: bool = False
    is_forced: bool = False


@dataclass(slots=True)
class AudioPreference:
    mode: str = "auto"
    title: str = ""
    lang: str = ""
    is_default: bool = False
    is_forced: bool = False


@dataclass(slots=True)
class UnifiedSubtitleOption:
    label: str
    mode: str
    track_id: int | None = None
    external_subtitle: ExternalSubtitleOption | None = None


@dataclass(slots=True)
class _PendingPlayItemLoad:
    index: int
    previous_index: int
    start_position_seconds: int
    pause: bool
    wait_for_load: bool


@dataclass(slots=True)
class _PendingPlaybackPrepare:
    index: int
    previous_index: int
    start_position_seconds: int
    pause: bool
    source_url: str
    requested_dash_video_id: str = ""
    previous_dash_video_id: str = ""
    previous_url: str = ""
    previous_original_url: str = ""
    previous_selected_playback_quality_id: str = ""


@dataclass(slots=True)
class _PendingPlaybackLoader:
    index: int
    previous_index: int
    start_position_seconds: int
    pause: bool


class PlayerWindow(QWidget, AsyncGuardMixin):
    _DASH_DATA_URI_PREFIX = "data:application/dash+xml;base64,"
    closed_to_main = Signal()
    _SEEK_SHORTCUT_SECONDS = 15
    _MODIFIED_SEEK_SHORTCUT_SECONDS = 60
    _VOLUME_SHORTCUT_STEP = 5
    _CURSOR_HIDE_DELAY_MS = 2000
    _MANUAL_SUBTITLE_SWITCH_REFRESH_WINDOW_SECONDS = 1.0
    _VIDEO_CONTEXT_MENU_DUPLICATE_WINDOW_MS = 250
    _VIDEO_CONTEXT_MENU_DUPLICATE_DISTANCE = 8
    _POSTER_SIZE = QSize(320, 154)
    _POSTER_FETCH_SIZE = QSize(360, 540)
    _POSTER_REQUEST_TIMEOUT_SECONDS = 10.0
    _SIDEBAR_MIN_WIDTH = 420
    _SIDEBAR_MAX_WIDTH = 620
    _PLAYLIST_GRID_SIZE = QSize(74, 36)
    _PLAYLIST_MIN_HEIGHT = 44
    _PLAYLIST_MAX_ROWS = 7
    _METADATA_MIN_HEIGHT = 112
    _METADATA_MAX_HEIGHT = 520
    _SIDEBAR_STYLESHEET = """
        QWidget#PlayerSidebarActions {
            background-color: transparent;
        }

        QPushButton#PlayerSidebarToggleButton {
            min-width: 32px;
            max-width: 32px;
            min-height: 30px;
            border: 0;
            border-radius: 8px;
            background-color: transparent;
            padding: 0;
        }

        QPushButton#PlayerSidebarToggleButton:hover {
            background-color: #e2ecf8;
        }

        QPushButton#PlayerSidebarToggleButton:checked {
            background-color: #dbeafe;
        }

        QComboBox#PlayerRouteCombo {
            min-height: 28px;
            border: 1px solid #cbd5e1;
            border-radius: 6px;
            background-color: #ffffff;
            padding: 2px 8px;
            color: #1f2937;
        }

        QComboBox#PlayerRouteCombo:focus {
            border-color: #409eff;
        }

        QTabBar#PlayerRouteTabs::tab {
            min-height: 28px;
            min-width: 74px;
            margin-right: 6px;
            padding: 2px 12px;
            border: 1px solid #d8dee8;
            border-radius: 8px;
            background-color: #ffffff;
            color: #334155;
        }

        QTabBar#PlayerRouteTabs::tab:hover {
            background-color: #eef6ff;
            border-color: #9ec8f3;
        }

        QTabBar#PlayerRouteTabs::tab:selected {
            background-color: #dbeafe;
            border-color: #409eff;
            color: #0f172a;
            font-weight: 600;
        }

        QWidget#PlayerPlaylistSection,
        QWidget#PlayerLogPanel {
            background-color: transparent;
            border: 0;
        }

        QListWidget#PlayerPlaylist {
            border: 0;
            border-radius: 0;
            background-color: transparent;
            padding: 0;
            color: #1f2937;
            outline: 0;
        }

        QListWidget#PlayerPlaylist::item {
            min-width: 54px;
            min-height: 26px;
            margin: 2px;
            padding: 0 4px;
            border: 1px solid #d8dee8;
            border-radius: 8px;
            background-color: #ffffff;
        }

        QListWidget#PlayerPlaylist::item:hover {
            background-color: #eef6ff;
            border-color: #9ec8f3;
        }

        QListWidget#PlayerPlaylist::item:selected {
            background-color: #dbeafe;
            border-color: #409eff;
            color: #0f172a;
        }

        QWidget#PlayerDetailsPanel {
            background-color: transparent;
            border: 0;
        }

        QLabel#PlayerPoster {
            background-color: #dbe4ef;
            border: 0;
            border-radius: 12px;
        }

        QLabel#PlayerDetailTitle {
            color: #0f172a;
            font-size: 16px;
            font-weight: 700;
        }

        QLabel#PlayerDetailSummary {
            color: #64748b;
            font-size: 12px;
        }

        QLabel#PlayerDetailSectionTitle {
            color: #111827;
            font-size: 13px;
            font-weight: 700;
            padding-top: 2px;
        }

        QTextBrowser#PlayerMetadataView {
            background-color: transparent;
            border: 0;
            padding: 0;
            color: #1f2937;
            selection-background-color: #cfe8ff;
        }

        QTextEdit#PlayerLogView {
            background-color: #ffffff;
            border: 1px solid #d8dee8;
            border-radius: 8px;
            padding: 8px;
            color: #1f2937;
            selection-background-color: #cfe8ff;
        }

        QTextEdit#PlayerLogView {
            color: #475569;
            font-family: Consolas, "Microsoft YaHei UI", monospace;
            font-size: 12px;
        }

        QPushButton#PlayerDetailActionButton {
            min-height: 30px;
            border: 1px solid #d8dee8;
            border-radius: 8px;
            background-color: #ffffff;
            padding: 4px 10px;
            color: #1f2937;
        }

        QPushButton#PlayerDetailActionButton:hover {
            background-color: #eef6ff;
            border-color: #9ec8f3;
        }

        QPushButton#PlayerDetailActionButton:checked {
            background-color: #e0f2fe;
            border-color: #38bdf8;
            color: #075985;
        }

        QPushButton#PlayerDetailActionButton:disabled {
            background-color: #f1f5f9;
            color: #94a3b8;
        }

        QSplitter::handle:vertical {
            height: 8px;
            background-color: transparent;
        }

        QSplitter::handle:vertical:hover {
            background-color: #dbeafe;
        }
    """
    _METADATA_DOCUMENT_STYLESHEET = """
        body {
            color: #1f2937;
            font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
            font-size: 13px;
            line-height: 1.45;
        }

        a {
            color: #0969da;
            text-decoration: none;
        }

        .meta-label {
            color: #64748b;
            font-size: 12px;
            font-weight: 600;
        }

        .meta-value {
            color: #1f2937;
            font-size: 13px;
        }

        .meta-empty {
            color: #94a3b8;
        }
    """
    _AUDIO_ONLY_SUFFIXES = {
        ".aac",
        ".aiff",
        ".alac",
        ".ape",
        ".flac",
        ".m4a",
        ".mp3",
        ".ogg",
        ".opus",
        ".wav",
        ".wma",
    }
    _DEFAULT_MAIN_SPLITTER_SIZES = [720, 560]
    _DANMAKU_SECONDARY_SCALE = 100
    _SUBTITLE_POSITION_PRESETS = {
        "顶部": 10,
        "偏上": 30,
        "默认": 50,
        "偏下": 70,
        "底部": 90,
    }
    _SUBTITLE_SCALE_PRESETS = {
        "很小": 70,
        "小": 85,
        "默认": 100,
        "大": 115,
        "很大": 130,
    }

    def __init__(
        self,
        controller,
        config=None,
        save_config=None,
        m3u8_ad_filter=None,
        playback_parser_service=None,
        default_video_cover_loader=None,
    ) -> None:
        super().__init__()
        self._init_async_guard()
        self.controller = controller
        self.config = config
        self._save_config = save_config or (lambda: None)
        self._m3u8_ad_filter = m3u8_ad_filter or M3U8AdFilter()
        self._playback_parser_service = playback_parser_service
        self._default_video_cover_loader = default_video_cover_loader
        self._default_video_cover_source: str | None = None
        self.session = None
        self.current_index = 0
        self.current_speed = 1.0
        self.is_playing = True
        self._is_muted = bool(getattr(self.config, "player_muted", False))
        self._was_maximized_before_fullscreen = False
        self._quit_requested = False
        self._video_pointer_inside = False
        self._app_event_filter_installed = False
        self._last_cursor_pos = None
        self._last_cursor_activity_ms = 0
        self._poster_request_id = 0
        self._video_poster_request_id = 0
        self._play_item_request_id = 0
        self._playback_loader_request_id = 0
        self._playback_prepare_request_id = 0
        self._detail_action_request_id = 0
        self._restore_saved_splitter_on_next_wide_exit = False
        self._pending_play_item_load: _PendingPlayItemLoad | None = None
        self._pending_playback_loader: _PendingPlaybackLoader | None = None
        self._pending_playback_prepare: _PendingPlaybackPrepare | None = None
        self._video_context_menu: QMenu | None = None
        self._danmaku_source_dialog: QDialog | None = None
        self._danmaku_settings_dialog: QDialog | None = None
        self._danmaku_source_title_edit: QLineEdit | None = None
        self._danmaku_source_episode_edit: QLineEdit | None = None
        self._danmaku_source_search_provider_combo: QComboBox | None = None
        self._danmaku_source_status_label: QLabel | None = None
        self._danmaku_source_provider_list: QListWidget | None = None
        self._danmaku_source_option_list: QListWidget | None = None
        self._danmaku_source_rerun_button: QPushButton | None = None
        self._danmaku_render_mode_combo: QComboBox | None = None
        self._danmaku_color_mode_combo: QComboBox | None = None
        self._danmaku_uniform_color_edit: QLineEdit | None = None
        self._danmaku_uniform_color_button: QPushButton | None = None
        self._danmaku_uniform_color_dialog: QColorDialog | None = None
        self._danmaku_uniform_color_preview_original: str | None = None
        self._danmaku_line_count_spin: QSpinBox | None = None
        self._danmaku_font_size_spin: QSpinBox | None = None
        self._danmaku_scroll_speed_spin: QDoubleSpinBox | None = None
        self._danmaku_position_preset_combo: QComboBox | None = None
        self._last_video_context_menu_request_ms = 0
        self._last_video_context_menu_request_global_pos: tuple[int, int] | None = None
        self._video_surface_ready = False
        self._video_picture_state = "idle"
        self._auto_advance_locked = False
        self._danmaku_track_id: int | None = None
        self._danmaku_temp_path: Path | None = None
        self._danmaku_active = False
        self._danmaku_line_count = 1
        self._danmaku_retry_attempts = 0
        self._primary_external_subtitle_retry_attempts = 0
        self._danmaku_loading_slot: str | None = None
        self._danmaku_uses_secondary_slot: bool | None = None
        self._danmaku_restore_ass_force_margins: str | None = None
        self._danmaku_restore_main_ass_override: str | None = None
        self._danmaku_restore_secondary_ass_override: str | None = None
        self._danmaku_restore_main_scale: int | None = None
        self._danmaku_restore_secondary_position: int | None = None
        self._danmaku_restore_secondary_scale: int | None = None
        self.help_dialog: ShortcutHelpDialog | None = None
        self._poster_load_signals = _PosterLoadSignals()
        self._connect_async_signal(self._poster_load_signals.loaded, self._handle_poster_load_finished)
        self._video_poster_load_signals = _PosterLoadSignals()
        self._connect_async_signal(self._video_poster_load_signals.loaded, self._handle_video_poster_load_finished)
        self._play_item_resolve_signals = _PlayItemResolveSignals()
        self._connect_async_signal(self._play_item_resolve_signals.succeeded, self._handle_play_item_resolve_succeeded)
        self._connect_async_signal(self._play_item_resolve_signals.failed, self._handle_play_item_resolve_failed)
        self._playback_loader_signals = _PlaybackLoaderSignals()
        self._connect_async_signal(self._playback_loader_signals.succeeded, self._handle_playback_loader_succeeded)
        self._connect_async_signal(self._playback_loader_signals.failed, self._handle_playback_loader_failed)
        self._detail_action_signals = _DetailActionSignals()
        self._connect_async_signal(self._detail_action_signals.succeeded, self._handle_detail_action_succeeded)
        self._connect_async_signal(self._detail_action_signals.failed, self._handle_detail_action_failed)
        self._playback_prepare_signals = _PlaybackPrepareSignals()
        self._connect_async_signal(self._playback_prepare_signals.succeeded, self._handle_playback_prepare_succeeded)
        self._connect_async_signal(self._playback_prepare_signals.failed, self._handle_playback_prepare_failed)
        self._background_task_signals = _BackgroundTaskSignals()
        self._connect_async_signal(self._background_task_signals.failed, self._append_log)
        self._danmaku_source_task_signals = _DanmakuSourceTaskSignals()
        self._connect_async_signal(self._danmaku_source_task_signals.finished, self._handle_danmaku_source_task_finished)
        self._danmaku_retry_timer = QTimer(self)
        self._danmaku_retry_timer.setSingleShot(True)
        self._danmaku_retry_timer.timeout.connect(self._retry_configure_danmaku_for_current_item)
        self._primary_external_subtitle_retry_timer = QTimer(self)
        self._primary_external_subtitle_retry_timer.setSingleShot(True)
        self._primary_external_subtitle_retry_timer.timeout.connect(self._retry_apply_primary_external_subtitle)
        self._pending_danmaku_timer = QTimer(self)
        self._pending_danmaku_timer.setInterval(300)
        self._pending_danmaku_timer.timeout.connect(self._refresh_pending_danmaku_for_current_item)
        self._controller_task_queue: queue.SimpleQueue[tuple[str, Callable[[], None]] | None] = queue.SimpleQueue()
        self._controller_task_worker = threading.Thread(
            target=self._run_controller_task_queue,
            daemon=True,
        )
        self._controller_task_worker.start()
        self.setWindowTitle(self._default_window_title())
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.resize(1280, 800)
        self.setMinimumSize(1000, 700)
        self._icons_dir = Path(__file__).resolve().parent.parent / "icons"
        if self.config and self.config.player_window_geometry:
            self.restoreGeometry(to_qbytearray(self.config.player_window_geometry))

        self.video_widget = MpvWidget(self)
        self._configure_video_surface_widgets()
        self.video_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.video_widget.customContextMenuRequested.connect(self._show_video_context_menu)
        self.video_widget.context_menu_requested.connect(self._show_video_context_menu_at_cursor)
        self.video_widget.context_menu_dismiss_requested.connect(self._dismiss_video_context_menu_at_cursor)
        self.video_widget.playback_failed.connect(self._handle_playback_failed)
        self.video_widget.video_picture_state_changed.connect(self._handle_video_picture_state_changed)
        self.video = self.video_widget
        self.playlist_group_combo = QComboBox()
        self.playlist_group_combo.setObjectName("PlayerRouteCombo")
        self.playlist_group_combo.setHidden(True)
        self.route_tab_bar = QTabBar()
        self.route_tab_bar.setObjectName("PlayerRouteTabs")
        self.route_tab_bar.setDrawBase(False)
        self.route_tab_bar.setExpanding(False)
        self.route_tab_bar.setUsesScrollButtons(True)
        self.route_tab_bar.setHidden(True)
        self.playlist = QListWidget()
        self.playlist.setObjectName("PlayerPlaylist")
        self.playlist.setSpacing(2)
        self.playlist.setViewMode(QListView.ViewMode.IconMode)
        self.playlist.setFlow(QListView.Flow.LeftToRight)
        self.playlist.setWrapping(True)
        self.playlist.setResizeMode(QListView.ResizeMode.Adjust)
        self.playlist.setMovement(QListView.Movement.Static)
        self.playlist.setUniformItemSizes(True)
        self.playlist.setGridSize(self._PLAYLIST_GRID_SIZE)
        self.playlist.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.play_button = self._create_icon_button("play.svg", "播放/暂停", "Space")
        self.prev_button = self._create_icon_button("previous.svg", "上一集", "PgUp")
        self.next_button = self._create_icon_button("next.svg", "下一集", "PgDn")
        self.backward_button = self._create_icon_button("seek-backward.svg", "后退", "Left")
        self.forward_button = self._create_icon_button("seek-forward.svg", "前进", "Right")
        self.refresh_button = self._create_icon_button("refresh.svg", "重新播放")
        self.mute_button = self._create_icon_button("volume-on.svg", "静音", "M")
        self.wide_button = self._create_icon_button("grid.svg", "宽屏", "W")
        self.fullscreen_button = self._create_icon_button("maximize.svg", "全屏", "Enter")
        self.wide_button.setCheckable(True)
        if self.config is not None:
            self.wide_button.setChecked(bool(self.config.player_wide_mode))
        self.toggle_playlist_button = self._create_icon_button("queue.svg", "播放列表")
        self.toggle_details_button = self._create_icon_button("info.svg", "详情")
        self.toggle_log_button = self._create_icon_button("log.svg", "日志")
        self.toggle_playlist_button.setObjectName("PlayerSidebarToggleButton")
        self.toggle_details_button.setObjectName("PlayerSidebarToggleButton")
        self.toggle_log_button.setObjectName("PlayerSidebarToggleButton")
        self.toggle_playlist_button.setFixedHeight(32)
        self.toggle_details_button.setFixedHeight(32)
        self.toggle_log_button.setFixedHeight(32)
        self.toggle_playlist_button.setFixedWidth(32)
        self.toggle_details_button.setFixedWidth(32)
        self.toggle_log_button.setFixedWidth(32)
        self.danmaku_source_button = self._create_icon_button("danmaku.svg", "弹幕源", "D")
        self.danmaku_settings_button = self._create_icon_button("sliders.svg", "弹幕设置", "Ctrl+D")
        self.toggle_playlist_button.setCheckable(True)
        self.toggle_details_button.setCheckable(True)
        self.toggle_log_button.setCheckable(True)
        self.toggle_playlist_button.setChecked(True)
        self.toggle_details_button.setChecked(True)
        self.toggle_log_button.setChecked(False)

        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "2.0x"])
        self.speed_combo.setCurrentText("1.0x")
        self._subtitle_tracks: list[SubtitleTrack] = []
        self._unified_primary_subtitle_options: list[UnifiedSubtitleOption] = []
        self._subtitle_preference = SubtitlePreference()
        self._secondary_subtitle_preference = SecondarySubtitlePreference()
        self._primary_external_subtitle_selection: ExternalSubtitleSelection | None = None
        self._secondary_external_subtitle_selection: ExternalSubtitleSelection | None = None
        self._primary_external_subtitle_track_id: int | None = None
        self._secondary_external_subtitle_track_id: int | None = None
        self._primary_external_subtitle_path: Path | None = None
        self._secondary_external_subtitle_path: Path | None = None
        self._main_subtitle_position = 50
        self._secondary_subtitle_position = 50
        self._secondary_subtitle_position_supported = False
        self._main_subtitle_scale = 100
        self._secondary_subtitle_scale = 100
        self._main_subtitle_scale_supported = False
        self._secondary_subtitle_scale_supported = False
        self._manual_subtitle_switch_refresh_until = 0.0
        self._skip_audio_refresh_for_manual_subtitle_switch = False
        self._auto_spider_subtitle_suppressed = False
        self._auto_spider_subtitle_attempted_key: tuple[int, str] | None = None
        self.subtitle_combo = QComboBox()
        self.subtitle_combo.addItem("自动选择", ("auto", None))
        self.subtitle_combo.setEnabled(False)
        self.danmaku_combo = QComboBox()
        self._reset_danmaku_combo()
        self._video_quality_options: list[VideoQualityOption] = []
        self.video_quality_combo = QComboBox()
        self._reset_video_quality_combo()
        self._audio_tracks: list[AudioTrack] = []
        self._audio_preference = AudioPreference()
        self.audio_combo = QComboBox()
        self.audio_combo.addItem("自动选择", ("auto", None))
        self.audio_combo.setEnabled(False)
        self.parse_combo = QComboBox()
        self.opening_spin = self._create_skip_spinbox("片头 ")
        self.ending_spin = self._create_skip_spinbox("片尾 ")

        self.current_time_label = QLabel("00:00")
        self.current_time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.duration_label = QLabel("00:00")
        self.duration_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress = ClickableSlider(Qt.Orientation.Horizontal)
        self.progress.set_hover_tooltip_formatter(self._format_time)
        self.progress.setFixedHeight(24)
        self.progress.setCursor(Qt.CursorShape.PointingHandCursor)
        self.volume_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.volume_slider.set_hover_tooltip_formatter(lambda value: f"{value}%")
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        initial_volume = 100
        if self.config is not None:
            initial_volume = max(
                self.volume_slider.minimum(),
                min(getattr(self.config, "player_volume", 100), self.volume_slider.maximum()),
            )
        self.volume_slider.setValue(initial_volume)
        self.volume_slider.setMaximumWidth(180)
        self.poster_label = QLabel()
        self.poster_label.setObjectName("PlayerPoster")
        self.poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster_label.setMinimumSize(self._POSTER_SIZE)
        self.poster_label.setMaximumSize(self._POSTER_SIZE)
        self.poster_label.setText("")
        self.poster_label.hide()
        self.video_poster_overlay = QLabel()
        self.video_poster_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_poster_overlay.setText("")
        self.video_poster_overlay.hide()
        self.metadata_view = QTextBrowser()
        self.metadata_view.setObjectName("PlayerMetadataView")
        self.metadata_view.setReadOnly(True)
        self.metadata_view.setOpenLinks(False)
        self.metadata_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.metadata_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.metadata_view.setMinimumHeight(128)
        self.metadata_view.document().setDocumentMargin(0)
        self.metadata_view.document().setDefaultStyleSheet(self._METADATA_DOCUMENT_STYLESHEET)
        self.metadata_view.anchorClicked.connect(self._handle_metadata_link)
        self.log_view = QTextEdit()
        self.log_view.setObjectName("PlayerLogView")
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(56)
        self.details = QWidget()
        self.details.setObjectName("PlayerDetailsPanel")
        self.details.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(0, 2, 0, 0)
        details_layout.setSpacing(8)
        details_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.detail_title_label = QLabel("")
        self.detail_title_label.setObjectName("PlayerDetailTitle")
        self.detail_title_label.setWordWrap(True)
        details_layout.addWidget(self.detail_title_label)
        self.detail_summary_label = QLabel("")
        self.detail_summary_label.setObjectName("PlayerDetailSummary")
        self.detail_summary_label.setWordWrap(True)
        self.detail_summary_label.hide()
        self.detail_actions_widget = QWidget()
        self.detail_actions_widget.setObjectName("PlayerDetailActions")
        self.detail_actions_layout = QHBoxLayout(self.detail_actions_widget)
        self.detail_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_actions_layout.setSpacing(8)
        details_layout.addWidget(self.detail_actions_widget)
        self.detail_fields_widget = QWidget()
        self.detail_fields_widget.setObjectName("PlayerDetailFields")
        self.detail_fields_layout = QVBoxLayout(self.detail_fields_widget)
        self.detail_fields_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_fields_layout.setSpacing(6)
        details_layout.addWidget(self.detail_fields_widget)
        self.metadata_title_label = self._create_detail_section_label("影片详情")
        details_layout.addWidget(self.metadata_title_label)
        details_layout.addWidget(self.metadata_view, 3)

        self.playlist_section = QWidget()
        self.playlist_section.setObjectName("PlayerPlaylistSection")
        playlist_section_layout = QVBoxLayout(self.playlist_section)
        playlist_section_layout.setContentsMargins(0, 0, 0, 0)
        playlist_section_layout.setSpacing(8)
        self.route_title_label = self._create_detail_section_label("播放线路")
        playlist_section_layout.addWidget(self.route_title_label)
        playlist_section_layout.addWidget(self.route_tab_bar)
        playlist_section_layout.addWidget(self.playlist_group_combo)
        self.episode_title_label = self._create_detail_section_label("选集")
        playlist_section_layout.addWidget(self.episode_title_label)
        playlist_section_layout.addWidget(self.playlist)

        self.log_panel = QWidget()
        self.log_panel.setObjectName("PlayerLogPanel")
        log_panel_layout = QVBoxLayout(self.log_panel)
        log_panel_layout.setContentsMargins(0, 0, 0, 0)
        log_panel_layout.setSpacing(8)
        self.log_title_label = self._create_detail_section_label("播放日志")
        log_panel_layout.addWidget(self.log_title_label)
        log_panel_layout.addWidget(self.log_view)

        self.report_timer = QTimer(self)
        self.report_timer.setInterval(5000)
        self.report_timer.timeout.connect(self.report_progress)
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(1000)
        self.progress_timer.timeout.connect(self._sync_progress_slider)
        self._cursor_hide_timer = QTimer(self)
        self._cursor_hide_timer.setInterval(100)
        self._cursor_hide_timer.timeout.connect(self._poll_cursor_idle_state)
        self._slider_dragging = False

        self.sidebar_actions_widget = QWidget()
        self.sidebar_actions_widget.setObjectName("PlayerSidebarActions")
        sidebar_actions = QHBoxLayout(self.sidebar_actions_widget)
        sidebar_actions.setContentsMargins(0, 0, 0, 0)
        sidebar_actions.setSpacing(8)
        sidebar_actions.addStretch(1)
        sidebar_actions.addWidget(self.toggle_playlist_button)
        sidebar_actions.addWidget(self.toggle_details_button)
        sidebar_actions.addWidget(self.toggle_log_button)

        self.bottom_area = QWidget()
        self.bottom_area.setMaximumHeight(72)
        bottom_layout = QVBoxLayout(self.bottom_area)
        self.bottom_layout = bottom_layout
        bottom_layout.setContentsMargins(12, 6, 12, 6)
        bottom_layout.setSpacing(4)

        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.addWidget(self.current_time_label)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.duration_label)
        bottom_layout.addLayout(progress_row)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addStretch(1)

        control_group = QWidget()
        control_group_layout = QHBoxLayout(control_group)
        control_group_layout.setContentsMargins(0, 0, 0, 0)
        control_group_layout.addWidget(self.prev_button)
        control_group_layout.addWidget(self.play_button)
        control_group_layout.addWidget(self.next_button)
        control_group_layout.addWidget(self.backward_button)
        control_group_layout.addWidget(self.forward_button)
        control_group_layout.addWidget(self.refresh_button)
        control_group_layout.addWidget(self.wide_button)
        control_group_layout.addWidget(self.fullscreen_button)
        control_group_layout.addWidget(self.danmaku_source_button)
        control_group_layout.addWidget(self.danmaku_settings_button)
        control_group_layout.addWidget(self.speed_combo)
        control_group_layout.addWidget(self.subtitle_combo)
        control_group_layout.addWidget(self.danmaku_combo)
        control_group_layout.addWidget(self.video_quality_combo)
        control_group_layout.addWidget(self.audio_combo)
        control_group_layout.addWidget(self.parse_combo)
        control_group_layout.addWidget(self.opening_spin)
        control_group_layout.addWidget(self.ending_spin)
        controls.addWidget(control_group, 0, Qt.AlignmentFlag.AlignCenter)
        controls.addStretch(1)

        volume_group = QWidget()
        self.volume_layout = QHBoxLayout(volume_group)
        self.volume_layout.setContentsMargins(0, 0, 0, 0)
        self.volume_layout.addWidget(self.mute_button)
        self.volume_layout.addWidget(self.volume_slider)
        controls.addWidget(volume_group, 0, Qt.AlignmentFlag.AlignRight)
        bottom_layout.addLayout(controls)

        video_container = QWidget()
        video_layout = QVBoxLayout(video_container)
        video_layout.setContentsMargins(0, 0, 0, 0)
        self.video_stack = QWidget()
        self.video_stack_layout = QStackedLayout(self.video_stack)
        self.video_stack_layout.setContentsMargins(0, 0, 0, 0)
        self.video_stack_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.video_stack_layout.addWidget(self.video_widget)
        self.video_stack_layout.addWidget(self.video_poster_overlay)
        video_layout.addWidget(self.video_stack)

        self.sidebar_splitter = QSplitter(Qt.Orientation.Vertical)
        self.sidebar_splitter.addWidget(self.details)
        self.sidebar_splitter.addWidget(self.playlist_section)
        self.sidebar_splitter.addWidget(self.log_panel)
        self.sidebar_splitter.setChildrenCollapsible(True)
        self.sidebar_splitter.setHandleWidth(8)
        self.sidebar_splitter.setStretchFactor(0, 2)
        self.sidebar_splitter.setStretchFactor(1, 1)
        self.sidebar_splitter.setStretchFactor(2, 0)

        sidebar_layout = QVBoxLayout()
        sidebar_layout.setContentsMargins(10, 10, 10, 10)
        sidebar_layout.setSpacing(8)
        sidebar_layout.addWidget(self.sidebar_actions_widget)
        sidebar_layout.addWidget(self.sidebar_splitter)
        self.sidebar_container = PosterBackgroundWidget()
        self.sidebar_container.setObjectName("PlayerSidebar")
        self.sidebar_container.setMinimumWidth(self._SIDEBAR_MIN_WIDTH)
        self.sidebar_container.setMaximumWidth(self._SIDEBAR_MAX_WIDTH)
        self.sidebar_container.setLayout(sidebar_layout)
        self.sidebar_container.setStyleSheet(self._SIDEBAR_STYLESHEET)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.addWidget(video_container)
        self.main_splitter.addWidget(self.sidebar_container)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 1)
        self._restore_main_splitter_state()
        self._sidebar_sizes = self.main_splitter.sizes()
        if self.wide_button.isChecked():
            self._restore_saved_splitter_on_next_wide_exit = bool(
                self.config is not None and self.config.player_main_splitter_state
            )
            self.main_splitter.setSizes([1, 0])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.main_splitter, 1)
        layout.addWidget(self.bottom_area, 0)

        self.play_button.clicked.connect(self.toggle_playback)
        self.prev_button.clicked.connect(self.play_previous)
        self.next_button.clicked.connect(self.play_next)
        self.backward_button.clicked.connect(lambda: self._seek_relative(-self._SEEK_SHORTCUT_SECONDS))
        self.forward_button.clicked.connect(lambda: self._seek_relative(self._SEEK_SHORTCUT_SECONDS))
        self.refresh_button.clicked.connect(self._replay_current_item)
        self.mute_button.clicked.connect(self._toggle_mute)
        self.wide_button.clicked.connect(self._toggle_wide_mode)
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self.speed_combo.currentTextChanged.connect(self._change_speed)
        self.subtitle_combo.currentIndexChanged.connect(self._change_subtitle_selection)
        self.danmaku_combo.currentIndexChanged.connect(self._change_danmaku_selection)
        self.video_quality_combo.currentIndexChanged.connect(self._change_video_quality_selection)
        self.audio_combo.currentIndexChanged.connect(self._change_audio_selection)
        self.parse_combo.currentIndexChanged.connect(self._change_parse_selection)
        self.opening_spin.valueChanged.connect(self._change_opening_seconds)
        self.ending_spin.valueChanged.connect(self._change_ending_seconds)
        self.volume_slider.valueChanged.connect(self._change_volume)
        self.playlist_group_combo.currentIndexChanged.connect(self._change_playlist_group)
        self.route_tab_bar.currentChanged.connect(self._change_playlist_group)
        self.playlist.itemDoubleClicked.connect(self._play_clicked_item)
        self.toggle_playlist_button.clicked.connect(self._update_sidebar_visibility)
        self.toggle_details_button.clicked.connect(self._update_sidebar_visibility)
        self.toggle_log_button.clicked.connect(self._update_sidebar_visibility)
        self.danmaku_source_button.clicked.connect(self._open_danmaku_source_dialog)
        self.danmaku_settings_button.clicked.connect(self._open_danmaku_settings_dialog)
        self.video_widget.double_clicked.connect(self.toggle_fullscreen)
        self.video_widget.playback_finished.connect(self._handle_playback_finished)
        self.video_widget.subtitle_tracks_changed.connect(self._refresh_subtitle_state)
        self.video_widget.audio_tracks_changed.connect(self._refresh_audio_state)
        self.progress.sliderPressed.connect(self._handle_slider_pressed)
        self.progress.sliderReleased.connect(self._seek_from_slider)
        self.progress.clicked_value.connect(self._seek_to_position)
        self.quit_shortcut = QShortcut(QKeySequence.StandardKey.Quit, self)
        self.quit_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.quit_shortcut.activated.connect(self._quit_application)
        self.return_shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
        self.return_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.return_shortcut.activated.connect(self._return_to_main)
        self.escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.escape_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.escape_shortcut.activated.connect(self._handle_escape)
        self.help_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F1), self)
        self.help_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.help_shortcut.activated.connect(self._show_shortcut_help)
        self._shortcut_bindings: list[QShortcut] = []
        self._register_shortcuts()
        self._update_play_button_icon()
        self._update_mute_button_icon()
        self._populate_parse_combo()
        self._apply_visibility_state()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._app_event_filter_installed = True

    def _format_tooltip(self, label: str, shortcut: str | None = None) -> str:
        if shortcut is None:
            return label
        return f"{label} ({shortcut})"

    def _create_icon_button(self, icon_name: str, tooltip: str, shortcut: str | None = None) -> QPushButton:
        button = QPushButton("")
        button.setToolTip(self._format_tooltip(tooltip, shortcut))
        button.setIcon(load_icon(self._icons_dir / icon_name))
        button.setIconSize(button.iconSize())
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedHeight(28)
        return button

    def _create_detail_section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("PlayerDetailSectionTitle")
        return label

    def _create_skip_spinbox(self, prefix: str) -> QSpinBox:
        spinbox = QSpinBox()
        spinbox.setPrefix(prefix)
        spinbox.setSuffix("s")
        spinbox.setRange(0, 240)
        spinbox.setFixedHeight(28)
        spinbox.setSingleStep(10)
        return spinbox

    def _update_play_button_icon(self) -> None:
        icon_name = "pause.svg" if self.is_playing else "play.svg"
        self.play_button.setIcon(load_icon(self._icons_dir / icon_name))

    def _default_window_title(self) -> str:
        return "alist-tvbox 播放器"

    def _active_playback_title(self) -> str:
        if self.session is None or not self.session.playlist:
            return self._default_window_title()
        current_item = self.session.playlist[self.current_index]
        parts = [self.session.vod.vod_name.strip(), current_item.title.strip()]
        parts = [part for part in parts if part]
        if not parts:
            return self._default_window_title()
        return " - ".join(parts)

    def _refresh_window_title(self) -> None:
        if not self.is_playing:
            self.setWindowTitle(self._default_window_title())
            return
        self.setWindowTitle(self._active_playback_title())

    def _session_playlists(self) -> list[list[PlayItem]]:
        if self.session is None:
            return []
        if self.session.playlists:
            return self.session.playlists
        return [self.session.playlist]

    def _playlist_group_label(self, playlist: list[PlayItem], playlist_index: int) -> str:
        if playlist and playlist[0].play_source:
            return playlist[0].play_source
        return f"线路 {playlist_index + 1}"

    def _render_playlist_group_combo(self) -> None:
        playlists = self._session_playlists()
        self.playlist_group_combo.blockSignals(True)
        self.route_tab_bar.blockSignals(True)
        self.playlist_group_combo.clear()
        while self.route_tab_bar.count():
            self.route_tab_bar.removeTab(0)
        for index, playlist in enumerate(playlists):
            label = self._playlist_group_label(playlist, index)
            self.playlist_group_combo.addItem(label)
            self.route_tab_bar.addTab(label)
        has_multiple_groups = len(playlists) > 1
        should_show_single_group_label = (
            len(playlists) == 1 and bool(playlists[0]) and bool(playlists[0][0].play_source)
        )
        should_show_route_selector = has_multiple_groups or should_show_single_group_label
        self.playlist_group_combo.setHidden(True)
        self.route_tab_bar.setHidden(not should_show_route_selector)
        self.route_title_label.setHidden(not should_show_route_selector)
        if self.session is not None and playlists:
            self.playlist_group_combo.setCurrentIndex(self.session.playlist_index)
            self.route_tab_bar.setCurrentIndex(self.session.playlist_index)
        self.route_tab_bar.blockSignals(False)
        self.playlist_group_combo.blockSignals(False)

    def _render_playlist_items(self) -> None:
        self.playlist.clear()
        if self.session is None:
            self._update_playlist_height()
            return
        for item in self.session.playlist:
            widget_item = QListWidgetItem(item.title)
            widget_item.setToolTip(item.title)
            widget_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.playlist.addItem(widget_item)
        self.playlist.setCurrentRow(self.current_index)
        self._update_playlist_height()

    def _playlist_column_count(self) -> int:
        grid_width = max(self._PLAYLIST_GRID_SIZE.width(), 1)
        available_width = self.playlist.viewport().width()
        if available_width <= 0:
            sidebar_width = self.sidebar_container.width() if hasattr(self, "sidebar_container") else 0
            available_width = sidebar_width - 20 if sidebar_width > 0 else self._SIDEBAR_MIN_WIDTH
        return max(1, available_width // grid_width)

    def _update_playlist_height(self) -> None:
        item_count = self.playlist.count()
        if item_count <= 0:
            height = self._PLAYLIST_MIN_HEIGHT
        else:
            columns = self._playlist_column_count()
            rows = (item_count + columns - 1) // columns
            visible_rows = min(rows, self._PLAYLIST_MAX_ROWS)
            height = visible_rows * self._PLAYLIST_GRID_SIZE.height() + 12
            height = max(self._PLAYLIST_MIN_HEIGHT, height)
        self.playlist.setMaximumHeight(height)
        if hasattr(self, "sidebar_splitter"):
            details_height = 0 if self.details.isHidden() else max(self.details.sizeHint().height(), 1)
            playlist_height = 0 if self.playlist_section.isHidden() else max(self.playlist_section.sizeHint().height(), height)
            log_height = 0 if self.log_panel.isHidden() else max(self.log_panel.sizeHint().height(), 1)
            self.sidebar_splitter.setSizes(
                [
                    details_height,
                    playlist_height,
                    log_height,
                ]
            )

    def _current_detail_actions(self) -> list[PlaybackDetailAction]:
        if self.session is None or not (0 <= self.current_index < len(self.session.playlist)):
            return []
        return [action for action in self.session.playlist[self.current_index].detail_actions if action.visible]

    def _current_detail_fields(self) -> list[PlaybackDetailField]:
        if self.session is None:
            return []
        if 0 <= self.current_index < len(self.session.playlist):
            item_fields = self.session.playlist[self.current_index].detail_fields
            if item_fields:
                return list(item_fields)
        return list(self.session.vod.detail_fields)

    def _clear_detail_action_buttons(self) -> None:
        while self.detail_actions_layout.count():
            item = self.detail_actions_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _clear_detail_field_rows(self) -> None:
        while self.detail_fields_layout.count():
            item = self.detail_fields_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _render_detail_fields(self) -> None:
        self._clear_detail_field_rows()
        self.detail_fields_widget.setHidden(True)

    def _render_detail_actions(self) -> None:
        self._clear_detail_action_buttons()
        actions = self._current_detail_actions()
        self.detail_actions_widget.setHidden(not actions)
        for action in actions:
            button = QPushButton(action.label)
            button.setObjectName("PlayerDetailActionButton")
            button.setToolTip(action.tooltip)
            button.setEnabled(action.enabled)
            button.setCheckable(True)
            button.setChecked(action.active)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty("detail_action_base_enabled", action.enabled)
            button.clicked.connect(lambda _checked=False, action_id=action.id: self._run_detail_action(action_id))
            self.detail_actions_layout.addWidget(button)

    def _run_detail_field_action(self, action: PlaybackDetailFieldAction) -> None:
        if self.session is None or self.session.detail_field_runner is None:
            return
        if not (0 <= self.current_index < len(self.session.playlist)):
            return
        current_item = self.session.playlist[self.current_index]
        try:
            self.session.detail_field_runner(current_item, action)
        except Exception as exc:
            self._append_log(f"详情跳转失败[{action.type}]: {exc}")

    def _detail_field_plain_text(self, field: PlaybackDetailField) -> str:
        values = " / ".join(part.label for part in field.value_parts)
        return f"{field.label}: {values}".rstrip()

    def _metadata_action_url(self, action: PlaybackDetailFieldAction) -> QUrl:
        url = QUrl("atv-player://detail-field")
        query = QUrlQuery()
        if action.target:
            query.addQueryItem("action_target", action.target)
        query.addQueryItem("action_type", action.type)
        query.addQueryItem("action_value", action.value)
        url.setQuery(query)
        return url

    def _metadata_action_from_payload(self, payload: object) -> PlaybackDetailFieldAction | None:
        if not isinstance(payload, dict):
            return None
        action_type = str(payload.get("type") or "").strip()
        action_value = str(payload.get("value") or "").strip()
        action_target = str(payload.get("target") or "").strip()
        if not action_type or not action_value:
            return None
        if action_target not in {"", "bilibili"}:
            return None
        return PlaybackDetailFieldAction(type=action_type, value=action_value, target=action_target)

    def _render_metadata_value_html(self, value: object) -> str:
        text = str(value or "")
        if not text:
            return ""

        parts: list[str] = []
        start = 0
        for match in _INLINE_METADATA_CR_RE.finditer(text):
            plain_chunk = text[start:match.start()]
            if plain_chunk:
                parts.append(html.escape(plain_chunk).replace("\n", "<br>"))

            action = None
            try:
                payload = json.loads(match.group("payload"))
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                action = self._metadata_action_from_payload(payload)

            if action is None:
                parts.append(html.escape(match.group(0)).replace("\n", "<br>"))
            else:
                href = html.escape(self._metadata_action_url(action).toString())
                label = html.escape(match.group("label"))
                parts.append(f'<a href="{href}">{label}</a>')
            start = match.end()

        tail = text[start:]
        if tail:
            parts.append(html.escape(tail).replace("\n", "<br>"))
        return "".join(parts)

    def _metadata_row_html(self, label: str, value: object) -> str:
        text = str(value or "")
        label_html = f'<span class="meta-label">{html.escape(label)}:</span>'
        if "[a=cr:" not in text:
            trimmed = text.rstrip()
            leading_spaces = len(trimmed) - len(trimmed.lstrip(" "))
            value_html = html.escape(trimmed[leading_spaces:]).replace("\n", "<br>")
            if leading_spaces:
                value_html = ("&nbsp;" * leading_spaces) + value_html
            if not value_html:
                return label_html
            return f'{label_html} <span class="meta-value">{value_html}</span>'.rstrip()
        return f'{label_html} <span class="meta-value">{self._render_metadata_value_html(text)}</span>'.rstrip()

    def _detail_field_html(self, field: PlaybackDetailField) -> str:
        parts: list[str] = []
        for part in field.value_parts:
            if part.action is None:
                parts.append(html.escape(part.label))
                continue
            href = html.escape(self._metadata_action_url(part.action).toString())
            label = html.escape(part.label)
            parts.append(f'<a href="{href}">{label}</a>')
        value_html = " / ".join(parts)
        if not value_html:
            return f'<span class="meta-label">{html.escape(field.label)}:</span>'
        return f'<span class="meta-label">{html.escape(field.label)}:</span> <span class="meta-value">{value_html}</span>'.rstrip()

    def _metadata_grid_html(self, entries: list[str]) -> str:
        filtered_entries = [entry for entry in entries if entry]
        if not filtered_entries:
            return ""
        rows: list[str] = []
        for index in range(0, len(filtered_entries), 2):
            left = filtered_entries[index]
            right = filtered_entries[index + 1] if index + 1 < len(filtered_entries) else ""
            rows.append(
                "<tr>"
                f'<td width="50%" style="padding: 0 12px 5px 0; vertical-align: top;">{left}</td>'
                f'<td width="50%" style="padding: 0 0 5px 8px; vertical-align: top;">{right}</td>'
                "</tr>"
            )
        return (
            '<table width="100%" cellspacing="0" cellpadding="0" style="border-collapse: collapse;">'
            + "".join(rows)
            + "</table>"
        )

    def _handle_metadata_link(self, url: QUrl) -> None:
        if url.scheme() != "atv-player" or url.host() != "detail-field":
            return
        query = QUrlQuery(url)
        action_target = query.queryItemValue("action_target", QUrl.ComponentFormattingOption.FullyDecoded).strip()
        action_type = query.queryItemValue("action_type", QUrl.ComponentFormattingOption.FullyDecoded).strip()
        action_value = query.queryItemValue("action_value", QUrl.ComponentFormattingOption.FullyDecoded).strip()
        if not action_type or not action_value:
            return
        if action_target not in {"", "bilibili"}:
            return
        self._run_detail_field_action(
            PlaybackDetailFieldAction(type=action_type, value=action_value, target=action_target)
        )

    def _set_detail_actions_enabled(self, enabled: bool) -> None:
        for index in range(self.detail_actions_layout.count()):
            widget = self.detail_actions_layout.itemAt(index).widget()
            if isinstance(widget, QPushButton):
                base_enabled = bool(widget.property("detail_action_base_enabled"))
                widget.setEnabled(enabled and base_enabled)

    def _set_button_icon(self, button: QPushButton, icon_name: str) -> None:
        icon: QIcon = load_icon(self._icons_dir / icon_name)
        button.setIcon(icon)

    def _update_mute_button_icon(self) -> None:
        icon_name = "volume-off.svg" if self._is_muted else "volume-on.svg"
        self._set_button_icon(self.mute_button, icon_name)

    def _video_surface_widgets(self) -> list[QWidget]:
        return [self.video_widget, *self.video_widget.findChildren(QWidget)]

    def _belongs_to_player_window(self, watched: object) -> bool:
        return isinstance(watched, QWidget) and (watched is self or watched.window() is self)

    def _configure_video_surface_widgets(self) -> None:
        for widget in self._video_surface_widgets():
            widget.setMouseTracking(True)
            widget.installEventFilter(self)
            widget.setCursor(Qt.CursorShape.ArrowCursor)

    def _set_video_cursor_hidden(self, hidden: bool) -> None:
        cursor_shape = Qt.CursorShape.BlankCursor if hidden else Qt.CursorShape.ArrowCursor
        for widget in self._video_surface_widgets():
            widget.setCursor(cursor_shape)
        self.setCursor(cursor_shape)

    def _restore_video_cursor(self, stop_timer: bool = True, disable_native_autohide: bool = True) -> None:
        if stop_timer:
            self._cursor_hide_timer.stop()
        self._set_video_cursor_hidden(False)
        if hasattr(self.video, "set_cursor_autohide"):
            if disable_native_autohide:
                self.video.set_cursor_autohide(None)
            elif self.is_playing:
                self.video.set_cursor_autohide(self._CURSOR_HIDE_DELAY_MS)

    def _cursor_now_ms(self) -> int:
        return int(time.monotonic() * 1000)

    def _handle_video_mouse_activity(self, now_ms: int | None = None) -> None:
        now_ms = self._cursor_now_ms() if now_ms is None else now_ms
        self._last_cursor_pos = QCursor.pos()
        self._last_cursor_activity_ms = now_ms
        self._set_video_cursor_hidden(False)
        if self.is_playing:
            if hasattr(self.video, "set_cursor_autohide"):
                self.video.set_cursor_autohide(self._CURSOR_HIDE_DELAY_MS)
            if not self._cursor_hide_timer.isActive():
                self._cursor_hide_timer.start()
            return
        self._restore_video_cursor()

    def _handle_video_leave(self) -> None:
        self._video_pointer_inside = False
        if self.is_playing:
            self._restore_video_cursor(stop_timer=False, disable_native_autohide=False)
            if not self._cursor_hide_timer.isActive():
                self._cursor_hide_timer.start()
            return
        self._restore_video_cursor()

    def _hide_video_cursor_if_idle(self) -> None:
        if self.is_playing and self._video_pointer_inside:
            self._set_video_cursor_hidden(True)

    def _refresh_video_pointer_inside_state(self) -> None:
        global_pos = QCursor.pos()
        local_pos = self.video_widget.mapFromGlobal(global_pos)
        self._video_pointer_inside = self.video_widget.rect().contains(local_pos)

    def _poll_cursor_idle_state(self, now_ms: int | None = None) -> None:
        now_ms = self._cursor_now_ms() if now_ms is None else now_ms
        global_pos = QCursor.pos()
        if self._last_cursor_pos is None or global_pos != self._last_cursor_pos:
            self._refresh_video_pointer_inside_state()
            self._handle_video_mouse_activity(now_ms=now_ms)
            return
        self._refresh_video_pointer_inside_state()
        if not self.is_playing:
            self._restore_video_cursor()
            return
        if not self._video_pointer_inside:
            self._restore_video_cursor(stop_timer=False, disable_native_autohide=False)
            if not self._cursor_hide_timer.isActive():
                self._cursor_hide_timer.start()
            return
        if hasattr(self.video, "set_cursor_autohide"):
            self.video.set_cursor_autohide(self._CURSOR_HIDE_DELAY_MS)
        if now_ms - self._last_cursor_activity_ms >= self._CURSOR_HIDE_DELAY_MS:
            self._set_video_cursor_hidden(True)

    def _sync_video_cursor_autohide(self) -> None:
        self._refresh_video_pointer_inside_state()
        if self.is_playing and self._video_pointer_inside:
            self._handle_video_mouse_activity()
            return
        if self.is_playing:
            self._cursor_hide_timer.stop()
            self._restore_video_cursor(disable_native_autohide=False)
            return
        self._restore_video_cursor()

    def open_session(self, session, start_paused: bool = False) -> None:
        self._invalidate_play_item_resolution()
        if not session.playlists:
            session.playlists = [session.playlist]
            session.playlist_index = 0
        self.session = session
        self.current_index = session.start_index
        self._render_poster()
        self._render_metadata()
        self._render_detail_fields()
        self._reset_log()
        self.current_speed = session.speed
        self.opening_spin.blockSignals(True)
        self.ending_spin.blockSignals(True)
        self.opening_spin.setValue(session.opening_seconds)
        self.ending_spin.setValue(session.ending_seconds)
        self.opening_spin.blockSignals(False)
        self.ending_spin.blockSignals(False)
        speed_text = self._speed_text(session.speed)
        speed_index = self.speed_combo.findText(speed_text)
        if speed_index >= 0:
            self.speed_combo.setCurrentIndex(speed_index)
        self.is_playing = not start_paused
        self._set_last_player_paused(start_paused)
        self._update_play_button_icon()
        self._refresh_window_title()
        self._render_playlist_group_combo()
        self._render_playlist_items()
        self._render_detail_actions()
        self._refresh_danmaku_source_entry_points()
        self.progress.setValue(0)
        self._reset_subtitle_combo()
        self._reset_danmaku_combo()
        self._reset_audio_combo()
        self._refresh_parse_combo_enabled_state()
        if session.initial_log_message:
            self._append_log(session.initial_log_message)
        self._handle_video_picture_state_changed("loading")
        if not session.playlist:
            self.report_timer.start()
            self.progress_timer.start()
            self._sync_video_cursor_autohide()
            return
        try:
            self._play_item_at_index(self.current_index, start_position_seconds=session.start_position_seconds, pause=start_paused)
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")
        self.report_timer.start()
        self.progress_timer.start()
        self._sync_video_cursor_autohide()

    def _video_load(
        self,
        url: str,
        pause: bool = False,
        start_seconds: int = 0,
        headers: dict[str, str] | None = None,
        poster_image_path: str | None = None,
    ) -> None:
        extra_kwargs: dict[str, object] = {}
        if headers:
            extra_kwargs["headers"] = headers
        if poster_image_path:
            extra_kwargs["poster_image_path"] = poster_image_path
        while True:
            try:
                self.video.load(url, pause=pause, start_seconds=start_seconds, **extra_kwargs)
                return
            except TypeError as exc:
                message = str(exc)
                removable = [key for key in tuple(extra_kwargs) if key in message]
                if not removable:
                    raise
                for key in removable:
                    extra_kwargs.pop(key, None)

    def _apply_playback_loader_result(self, load_result: PlaybackLoadResult | None) -> None:
        if self.session is None:
            return
        if not isinstance(load_result, PlaybackLoadResult) or not load_result.replacement_playlist:
            self._render_detail_actions()
            return
        replacement = list(load_result.replacement_playlist)
        self.session.playlists[self.session.playlist_index] = replacement
        self.session.playlist = replacement
        self.current_index = max(
            0,
            min(load_result.replacement_start_index, len(replacement) - 1),
        )
        self._render_playlist_group_combo()
        self._render_playlist_items()
        self._render_detail_actions()

    def _start_playback_loader(
        self,
        *,
        previous_index: int,
        start_position_seconds: int,
        pause: bool,
    ) -> None:
        if self.session is None or self.session.playback_loader is None:
            return
        current_item = self.session.playlist[self.current_index]
        playback_loader = self.session.playback_loader
        self._append_log(f"正在加载播放地址: {current_item.title}")
        self._playback_loader_request_id += 1
        request_id = self._playback_loader_request_id
        self._pending_playback_loader = _PendingPlaybackLoader(
            index=self.current_index,
            previous_index=previous_index,
            start_position_seconds=start_position_seconds,
            pause=pause,
        )

        def run() -> None:
            try:
                load_result = playback_loader(current_item)
            except Exception as exc:
                if self._is_window_alive():
                    self._playback_loader_signals.failed.emit(request_id, str(exc))
                return
            if not self._is_window_alive():
                return
            self._playback_loader_signals.succeeded.emit(request_id, load_result)

        threading.Thread(target=run, daemon=True).start()

    def _prepare_current_play_item(
        self,
        *,
        previous_index: int,
        start_position_seconds: int,
        pause: bool,
    ) -> bool:
        if self.session is None:
            return True
        current_item = self.session.playlist[self.current_index]
        resolved_vod = self._resolve_current_play_item()
        if self.session.playback_loader is not None:
            if self.session.async_playback_loader and not current_item.url:
                self._start_playback_loader(
                    previous_index=previous_index,
                    start_position_seconds=start_position_seconds,
                    pause=pause,
                )
                return False
            load_result = self.session.playback_loader(current_item)
            self._apply_playback_loader_result(load_result)
            self._render_poster()
            current_item = self.session.playlist[self.current_index]
        if current_item.url:
            if resolved_vod is None and current_item.vod_id and self.session.detail_resolver is not None:
                self._start_play_item_resolution(
                    previous_index=previous_index,
                    start_position_seconds=start_position_seconds,
                    pause=pause,
                    wait_for_load=False,
                )
            if self._start_playback_prepare(
                previous_index=previous_index,
                start_position_seconds=start_position_seconds,
                pause=pause,
            ):
                return False
            return True
        if current_item.vod_id and self.session.detail_resolver is not None:
            self._start_play_item_resolution(
                previous_index=previous_index,
                start_position_seconds=start_position_seconds,
                pause=pause,
                wait_for_load=True,
            )
            return False
        return True

    def _start_current_item_playback(self, start_position_seconds: int = 0, pause: bool = False) -> None:
        if self.session is None:
            return
        current_item = self.session.playlist[self.current_index]
        self._append_log(f"当前: {current_item.title}")
        self._append_log(f"URL: {current_item.url}")
        if start_position_seconds > self.opening_spin.value():
            effective_start_seconds = start_position_seconds
        else:
            effective_start_seconds = self.opening_spin.value()
        poster_image_path = self._preferred_audio_cover_path() if self._should_use_audio_cover(current_item.url) else None
        self._video_load(
            current_item.url,
            pause=pause,
            start_seconds=effective_start_seconds,
            headers=current_item.headers,
            poster_image_path=poster_image_path,
        )
        self._auto_advance_locked = False
        self._configure_video_surface_widgets()
        self.video.set_speed(self.current_speed)
        self.video.set_volume(self.volume_slider.value())
        self._apply_muted_state()
        self._refresh_subtitle_state()
        self._schedule_followup_subtitle_refresh_if_needed(current_item)
        self._refresh_audio_state()
        self._refresh_video_quality_state()
        self._configure_danmaku_for_current_item()

    def _schedule_followup_subtitle_refresh_if_needed(
        self,
        current_item: PlayItem,
        *,
        retries_remaining: int = 4,
    ) -> None:
        if not current_item.external_subtitles and self._primary_external_subtitle_selection is None:
            return

        def refresh_if_still_current() -> None:
            if self.session is None:
                return
            if self.current_index >= len(self.session.playlist):
                return
            if self.session.playlist[self.current_index] is not current_item:
                return
            self._refresh_subtitle_state()
            if retries_remaining <= 0 or not self._should_retry_followup_subtitle_refresh(current_item):
                return
            self._schedule_followup_subtitle_refresh_if_needed(
                current_item,
                retries_remaining=retries_remaining - 1,
            )

        QTimer.singleShot(150, refresh_if_still_current)

    def _load_current_item(
        self,
        start_position_seconds: int = 0,
        pause: bool = False,
        *,
        previous_index: int | None = None,
        preserve_primary_external_subtitle_selection: bool = False,
    ) -> None:
        if self.session is None:
            return
        self._invalidate_play_item_resolution()
        self._clear_manual_subtitle_switch_refresh()
        self._auto_spider_subtitle_suppressed = False
        self._auto_spider_subtitle_attempted_key = None
        self._clear_external_subtitle_tracks(
            preserve_primary_selection=preserve_primary_external_subtitle_selection,
        )
        self._clear_active_danmaku()
        self._reset_danmaku_combo()
        self._video_quality_options = []
        self._reset_video_quality_combo()
        self._refresh_parse_combo_enabled_state()
        if not self._prepare_current_play_item(
            previous_index=self.current_index if previous_index is None else previous_index,
            start_position_seconds=start_position_seconds,
            pause=pause,
        ):
            return
        self._refresh_parse_combo_enabled_state()
        self._start_current_item_playback(start_position_seconds=start_position_seconds, pause=pause)

    def _current_play_item_title(self) -> str:
        if self.session is None or not (0 <= self.current_index < len(self.session.playlist)):
            return ""
        return str(self.session.playlist[self.current_index].title or "").strip()

    def _render_detail_header(self) -> None:
        if self.session is None:
            self.detail_title_label.clear()
            self.detail_summary_label.clear()
            self.detail_summary_label.hide()
            return

        vod = self.session.vod
        item_title = self._current_play_item_title()
        title = str(vod.vod_name or item_title or "未命名影片").strip()
        self.detail_title_label.setText(title)
        self.detail_summary_label.clear()
        self.detail_summary_label.hide()

    def _format_metadata_text(self, vod) -> str:
        if getattr(vod, "detail_style", "") == "live":
            if getattr(vod, "epg_current", ""):
                lines = ["当前节目:", vod.epg_current]
                if getattr(vod, "epg_schedule", ""):
                    lines.extend(["", "今日节目单:", vod.epg_schedule])
                lines.extend(self._detail_field_plain_text(field) for field in self._current_detail_fields())
                return "\n".join(lines)
            rows = [
                ("标题", vod.vod_name),
                ("平台", vod.vod_director),
                ("类型", vod.type_name),
                ("主播", vod.vod_actor),
                ("人气", vod.vod_remarks),
            ]
            lines = [f"{label}: {value}".rstrip() for label, value in rows]
            lines.extend(self._detail_field_plain_text(field) for field in self._current_detail_fields())
            return "\n".join(lines)
        rows = [
            ("名称", vod.vod_name),
            ("类型", vod.type_name),
            ("年代", vod.vod_year),
            ("地区", vod.vod_area),
            ("语言", vod.vod_lang),
            ("评分", vod.vod_remarks),
            ("导演", vod.vod_director),
            ("演员", vod.vod_actor),
            ("豆瓣ID", str(vod.dbid) if vod.dbid else ""),
        ]
        if getattr(vod, "detail_style", "") == "bilibili":
            rows = [
                (label, value)
                for label, value in rows
                if label not in {"年代", "地区", "语言", "豆瓣ID"}
            ]
        lines = [f"{label}: {value}".rstrip() for label, value in rows]
        lines.extend(self._detail_field_plain_text(field) for field in self._current_detail_fields())
        lines.append("")
        lines.append("简介:")
        lines.append(vod.vod_content)
        return "\n".join(lines)

    def _format_metadata_html(self, vod) -> str:
        if getattr(vod, "detail_style", "") == "live":
            if getattr(vod, "epg_current", ""):
                parts = ['<span class="meta-label">当前节目:</span>', self._render_metadata_value_html(vod.epg_current)]
                if getattr(vod, "epg_schedule", ""):
                    parts.extend(["", '<span class="meta-label">今日节目单:</span>', self._render_metadata_value_html(vod.epg_schedule)])
                parts.extend(self._detail_field_html(field) for field in self._current_detail_fields())
                return "<br>".join(parts)
            rows = [
                ("标题", vod.vod_name),
                ("平台", vod.vod_director),
                ("类型", vod.type_name),
                ("主播", vod.vod_actor),
                ("人气", vod.vod_remarks),
            ]
            entries = [self._metadata_row_html(label, value) for label, value in rows]
            entries.extend(self._detail_field_html(field) for field in self._current_detail_fields())
            return self._metadata_grid_html(entries)
        rows = [
            ("名称", vod.vod_name),
            ("类型", vod.type_name),
            ("年代", vod.vod_year),
            ("地区", vod.vod_area),
            ("语言", vod.vod_lang),
            ("评分", vod.vod_remarks),
            ("导演", vod.vod_director),
            ("演员", vod.vod_actor),
            ("豆瓣ID", str(vod.dbid) if vod.dbid else ""),
        ]
        if getattr(vod, "detail_style", "") == "bilibili":
            rows = [
                (label, value)
                for label, value in rows
                if label not in {"年代", "地区", "语言", "豆瓣ID"}
            ]
        entries = [self._metadata_row_html(label, value) for label, value in rows]
        entries.extend(self._detail_field_html(field) for field in self._current_detail_fields())
        grid_html = self._metadata_grid_html(entries)
        intro_html = '<span class="meta-label">简介:</span><br>' + self._render_metadata_value_html(vod.vod_content)
        if not grid_html:
            return intro_html
        return grid_html + intro_html

    def _render_metadata(self) -> None:
        if self.session is None:
            self._render_detail_header()
            self.metadata_view.clear()
            self._update_metadata_height()
            return
        self._render_detail_header()
        self.metadata_view.setHtml(self._format_metadata_html(self.session.vod))
        QTimer.singleShot(0, self._update_metadata_height)

    def _update_metadata_height(self) -> None:
        if not hasattr(self, "metadata_view"):
            return
        width = self.metadata_view.viewport().width()
        if width > 0:
            self.metadata_view.document().setTextWidth(width)
        document_height = int(self.metadata_view.document().size().height()) + 12
        height = max(self._METADATA_MIN_HEIGHT, min(document_height, self._METADATA_MAX_HEIGHT))
        self.metadata_view.setMinimumHeight(height)
        self.metadata_view.setMaximumHeight(height)
        if hasattr(self, "sidebar_splitter"):
            self._update_playlist_height()

    def _apply_resolved_vod(self, resolved_vod: VodItem) -> None:
        if self.session is None:
            return
        self.session.vod = resolved_vod
        self._render_poster()
        self._render_metadata()
        self._render_detail_fields()

    def _resolve_current_play_item(self) -> VodItem | None:
        if self.session is None:
            return None
        current_item = self.session.playlist[self.current_index]
        if not current_item.vod_id or current_item.vod_id not in self.session.resolved_vod_by_id:
            return None
        resolved_vod = self.controller.resolve_play_item_detail(self.session, current_item)
        if resolved_vod is not None:
            self._apply_resolved_vod(resolved_vod)
        return resolved_vod

    def _play_item_at_index(
        self,
        index: int,
        start_position_seconds: int = 0,
        pause: bool = False,
        *,
        preserve_primary_external_subtitle_selection: bool = False,
    ) -> None:
        if self.session is None:
            return
        previous_index = self.current_index
        self.current_index = index
        try:
            self.playlist.setCurrentRow(self.current_index)
            self._refresh_danmaku_source_entry_points()
            self._render_metadata()
            self._render_detail_fields()
            self._render_detail_actions()
            self._load_current_item(
                start_position_seconds=start_position_seconds,
                pause=pause,
                previous_index=previous_index,
                preserve_primary_external_subtitle_selection=preserve_primary_external_subtitle_selection,
            )
            self._refresh_window_title()
        except Exception:
            self._restore_or_keep_current_index_after_failure(previous_index)
            raise

    def _clear_poster(self) -> None:
        self.poster_label.clear()
        self.poster_label.setText("")
        self.poster_label.setPixmap(QPixmap())
        self.sidebar_container.clear_background_pixmap()
        self._clear_video_poster_overlay()

    def _clear_video_poster_overlay(self) -> None:
        self.video_poster_overlay.clear()
        self.video_poster_overlay.setText("")
        self.video_poster_overlay.setPixmap(QPixmap())
        self.video_poster_overlay.hide()

    def _show_video_poster_overlay(self, pixmap: QPixmap) -> None:
        if pixmap.isNull() or self._video_surface_ready:
            self.video_poster_overlay.hide()
            return
        target_size = self.video_stack.size()
        if target_size.width() <= 0 or target_size.height() <= 0:
            target_size = self._POSTER_SIZE
        self.video_poster_overlay.setText("")
        self.video_poster_overlay.setPixmap(
            pixmap.scaled(
                target_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.video_poster_overlay.show()

    def _cover_scaled_pixmap(self, pixmap: QPixmap, target_size: QSize) -> QPixmap:
        if pixmap.isNull() or target_size.width() <= 0 or target_size.height() <= 0:
            return QPixmap()
        scaled = pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = max((scaled.width() - target_size.width()) // 2, 0)
        y = max((scaled.height() - target_size.height()) // 2, 0)
        return scaled.copy(x, y, target_size.width(), target_size.height())

    def _detail_poster_pixmap(self, pixmap: QPixmap) -> QPixmap:
        if pixmap.isNull():
            return QPixmap()
        target_size = self._POSTER_SIZE
        banner = QPixmap(target_size)
        banner.fill(QColor("#dbe4ef"))

        painter = QPainter(banner)
        try:
            background = self._cover_scaled_pixmap(pixmap, target_size)
            if not background.isNull():
                painter.drawPixmap(QRect(0, 0, target_size.width(), target_size.height()), background)
                painter.fillRect(QRect(0, 0, target_size.width(), target_size.height()), QColor(15, 23, 42, 96))

            inset_size = QSize(max(target_size.width() - 24, 1), max(target_size.height() - 20, 1))
            foreground = pixmap.scaled(
                inset_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = max((target_size.width() - foreground.width()) // 2, 0)
            y = max((target_size.height() - foreground.height()) // 2, 0)
            painter.drawPixmap(x, y, foreground)
        finally:
            painter.end()
        return banner

    def _load_poster_pixmap(self, source: str, *, cover: bool = False) -> QPixmap:
        if not source:
            return QPixmap()
        source_path = Path(source)
        if not source_path.is_file():
            return QPixmap()
        pixmap = QPixmap(str(source_path))
        if pixmap.isNull():
            return QPixmap()
        if cover:
            return self._detail_poster_pixmap(pixmap)
        return pixmap.scaled(
            self._POSTER_FETCH_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _start_poster_load(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        image_url = normalize_poster_url(source)
        if not image_url:
            return

        def load() -> None:
            image = load_remote_poster_image(
                image_url,
                self._POSTER_FETCH_SIZE,
                timeout=self._POSTER_REQUEST_TIMEOUT_SECONDS,
                get=httpx.get,
            )
            if self._is_window_alive():
                if target == "video":
                    self._video_poster_load_signals.loaded.emit(request_id, image)
                else:
                    self._poster_load_signals.loaded.emit(request_id, image)

        threading.Thread(target=load, daemon=True).start()

    def _handle_poster_load_finished(self, request_id: int, image: QImage | None) -> None:
        if request_id != self._poster_request_id:
            return
        if image is None or image.isNull():
            self.poster_label.clear()
            self.poster_label.setText("")
            self.poster_label.setPixmap(QPixmap())
            self.sidebar_container.clear_background_pixmap()
            return
        pixmap = QPixmap.fromImage(image)
        detail_pixmap = self._detail_poster_pixmap(pixmap)
        self.poster_label.setText("")
        self.poster_label.setPixmap(detail_pixmap)
        self.sidebar_container.set_background_pixmap(pixmap)
        if self._preferred_video_poster_source() == self._preferred_detail_poster_source():
            self._show_video_poster_overlay(pixmap)
            self._attach_audio_cover_if_available()

    def _handle_video_poster_load_finished(self, request_id: int, image: QImage | None) -> None:
        if request_id != self._video_poster_request_id:
            return
        if image is None or image.isNull():
            self._clear_video_poster_overlay()
            return
        pixmap = QPixmap.fromImage(image)
        self._show_video_poster_overlay(pixmap)
        self._attach_audio_cover_if_available()

    def _has_active_primary_external_subtitle(self) -> bool:
        return self._current_primary_external_subtitle() is not None and self._primary_external_subtitle_track_id is not None

    def _resolve_default_video_cover_source(self) -> str:
        if self._default_video_cover_source is not None:
            return self._default_video_cover_source
        loader = self._default_video_cover_loader
        if not callable(loader):
            self._default_video_cover_source = ""
            return ""
        try:
            self._default_video_cover_source = str(loader() or "")
        except Exception:
            self._default_video_cover_source = ""
        return self._default_video_cover_source

    def _preferred_detail_poster_source(self) -> str:
        if self.session is None:
            return ""
        return self.session.vod.vod_pic or ""

    def _preferred_video_poster_source(self) -> str:
        if self.session is None:
            return ""
        current_item = self._current_play_item()
        if current_item is not None and current_item.video_cover_override:
            return current_item.video_cover_override
        if self.session.video_cover_override:
            return self.session.video_cover_override
        if self.session.vod.vod_pic:
            return self.session.vod.vod_pic
        return self._resolve_default_video_cover_source()

    def _preferred_poster_source(self) -> str:
        return self._preferred_video_poster_source()

    def _should_use_audio_cover(self, url: str) -> bool:
        normalized_path = urlparse(url or "").path.lower()
        return any(normalized_path.endswith(suffix) for suffix in self._AUDIO_ONLY_SUFFIXES)

    def _preferred_audio_cover_path(self) -> str | None:
        source = self._preferred_video_poster_source().strip()
        if not source:
            return None
        source_path = Path(source)
        if source_path.is_file():
            return str(source_path)
        normalized = normalize_poster_url(source)
        if normalized.startswith(("http://", "https://")):
            cached_path = poster_cache_path(normalized)
            if cached_path.is_file():
                return str(cached_path)
        return None

    def _attach_audio_cover_if_available(self) -> None:
        if self.session is None or not hasattr(self.video, "attach_audio_cover"):
            return
        current_item = self._current_play_item()
        if current_item is None or not self._should_use_audio_cover(current_item.url):
            return
        poster_image_path = self._preferred_audio_cover_path()
        if not poster_image_path:
            return
        try:
            self.video.attach_audio_cover(poster_image_path)
        except Exception as exc:
            self._append_log(f"封面挂载失败: {exc}")

    def _render_detail_poster(self) -> None:
        self._poster_request_id += 1
        if self.session is None:
            self.poster_label.clear()
            self.poster_label.setText("")
            self.poster_label.setPixmap(QPixmap())
            self.sidebar_container.clear_background_pixmap()
            return
        source = self._preferred_detail_poster_source()
        if not source:
            self.poster_label.clear()
            self.poster_label.setText("")
            self.poster_label.setPixmap(QPixmap())
            self.sidebar_container.clear_background_pixmap()
            return
        background_pixmap = self._load_poster_pixmap(source)
        if not background_pixmap.isNull():
            pixmap = self._load_poster_pixmap(source, cover=True)
            self.poster_label.setText("")
            self.poster_label.setPixmap(pixmap if not pixmap.isNull() else background_pixmap)
            self.sidebar_container.set_background_pixmap(background_pixmap)
            return
        self.poster_label.clear()
        self.poster_label.setText("")
        self.poster_label.setPixmap(QPixmap())
        self.sidebar_container.clear_background_pixmap()
        self._start_poster_load(source, self._poster_request_id, target="detail")

    def _render_video_poster(self) -> None:
        self._video_poster_request_id += 1
        self._video_surface_ready = False
        if self.session is None:
            self._clear_video_poster_overlay()
            return
        source = self._preferred_video_poster_source()
        if not source:
            self._clear_video_poster_overlay()
            return
        detail_source = self._preferred_detail_poster_source()
        if source == detail_source:
            pixmap = self.sidebar_container.background_pixmap()
            if pixmap.isNull():
                pixmap = self.poster_label.pixmap()
            if pixmap is not None and not pixmap.isNull():
                self._show_video_poster_overlay(pixmap)
            else:
                self._clear_video_poster_overlay()
            return
        pixmap = self._load_poster_pixmap(source)
        if not pixmap.isNull():
            self._show_video_poster_overlay(pixmap)
            return
        self._clear_video_poster_overlay()
        self._start_poster_load(source, self._video_poster_request_id, target="video")

    def _render_poster(self) -> None:
        self._render_detail_poster()
        self._render_video_poster()

    def _handle_video_picture_state_changed(self, state: str) -> None:
        self._video_picture_state = state
        if state in {"visible", "audio-cover"}:
            self._video_surface_ready = True
            self.video_poster_overlay.hide()
            return
        self._video_surface_ready = False
        if state == "unavailable" and self._has_active_primary_external_subtitle():
            self.video_poster_overlay.hide()
            return
        pixmap = self.video_poster_overlay.pixmap()
        if pixmap is not None and not pixmap.isNull():
            self._show_video_poster_overlay(pixmap)

    def _handle_playback_failed(self, message: str) -> None:
        self._append_log(message)
        self._video_surface_ready = False
        pixmap = self.video_poster_overlay.pixmap()
        if pixmap is not None and not pixmap.isNull():
            self._show_video_poster_overlay(pixmap)

    def _reset_log(self) -> None:
        self.log_view.clear()

    def _append_log(self, message: str) -> None:
        if not message:
            return
        if self.log_view.toPlainText():
            self.log_view.append(message)
            return
        self.log_view.setPlainText(message)

    def append_status_log(self, message: str) -> None:
        self._append_log(message)

    def _set_last_player_paused(self, paused: bool) -> None:
        if self.config is None:
            return
        self.config.last_player_paused = paused
        self._save_config()

    def _is_window_alive(self) -> bool:
        return self._can_deliver_async_result()

    def _invalidate_play_item_resolution(self) -> None:
        self._play_item_request_id += 1
        self._pending_play_item_load = None
        self._playback_loader_request_id += 1
        self._pending_playback_loader = None
        self._playback_prepare_request_id += 1
        self._pending_playback_prepare = None

    def _run_controller_task_queue(self) -> None:
        while True:
            task_entry = self._controller_task_queue.get()
            if task_entry is None:
                return
            error_prefix, task = task_entry
            try:
                task()
            except Exception as exc:
                if self._is_window_alive():
                    self._background_task_signals.failed.emit(f"{error_prefix}: {exc}")

    def _enqueue_controller_task(self, error_prefix: str, task: Callable[[], None]) -> None:
        self._controller_task_queue.put((error_prefix, task))

    def _shutdown_controller_task_queue(self) -> None:
        self._controller_task_queue.put(None)

    def _run_detail_action(self, action_id: str) -> None:
        if self.session is None or self.session.detail_action_runner is None:
            self._append_log(f"详情动作未注册[{action_id}]")
            return
        if not (0 <= self.current_index < len(self.session.playlist)):
            return
        current_item = self.session.playlist[self.current_index]
        expected_index = self.current_index
        self._detail_action_request_id += 1
        request_id = self._detail_action_request_id
        self._set_detail_actions_enabled(False)

        def run() -> None:
            try:
                actions = self.session.detail_action_runner(current_item, action_id)
            except Exception as exc:
                if self._is_window_alive():
                    self._detail_action_signals.failed.emit(request_id, f"详情动作执行失败[{action_id}]: {exc}")
                return
            if self._is_window_alive():
                self._detail_action_signals.succeeded.emit(request_id, current_item, (expected_index, actions))

        threading.Thread(target=run, daemon=True).start()

    def _start_play_item_resolution(
        self,
        *,
        previous_index: int,
        start_position_seconds: int,
        pause: bool,
        wait_for_load: bool,
    ) -> None:
        if self.session is None:
            return
        session = self.session
        current_item = session.playlist[self.current_index]
        if wait_for_load:
            self._append_log(f"正在加载播放地址: {current_item.title}")
        self._play_item_request_id += 1
        request_id = self._play_item_request_id
        self._pending_play_item_load = _PendingPlayItemLoad(
            index=self.current_index,
            previous_index=previous_index,
            start_position_seconds=start_position_seconds,
            pause=pause,
            wait_for_load=wait_for_load,
        )

        def run() -> None:
            try:
                resolved_vod = self.controller.resolve_play_item_detail(session, current_item)
            except Exception as exc:
                if self._is_window_alive():
                    self._play_item_resolve_signals.failed.emit(request_id, str(exc))
                return
            if not self._is_window_alive():
                return
            self._play_item_resolve_signals.succeeded.emit(request_id, resolved_vod)

        threading.Thread(target=run, daemon=True).start()

    def _start_playback_prepare(
        self,
        *,
        previous_index: int,
        start_position_seconds: int,
        pause: bool,
        dash_video_id: str | None = None,
        previous_url: str = "",
        previous_original_url: str = "",
        previous_selected_playback_quality_id: str = "",
    ) -> bool:
        if self.session is None:
            return False
        current_item = self.session.playlist[self.current_index]
        source_url = self._playback_prepare_source_url(current_item)
        if source_url.startswith(self._DASH_DATA_URI_PREFIX) and not current_item.original_url:
            current_item.original_url = source_url
        should_prepare = getattr(self._m3u8_ad_filter, "should_prepare", None)
        if callable(should_prepare):
            if not should_prepare(source_url):
                return False
        elif ".m3u8" not in source_url.lower():
            return False
        self._playback_prepare_request_id += 1
        request_id = self._playback_prepare_request_id
        requested_dash_video_id = dash_video_id if dash_video_id is not None else current_item.dash_video_id
        self._pending_playback_prepare = _PendingPlaybackPrepare(
            index=self.current_index,
            previous_index=previous_index,
            start_position_seconds=start_position_seconds,
            pause=pause,
            source_url=source_url,
            requested_dash_video_id=requested_dash_video_id,
            previous_dash_video_id=current_item.dash_video_id,
            previous_url=previous_url,
            previous_original_url=previous_original_url,
            previous_selected_playback_quality_id=previous_selected_playback_quality_id,
        )

        def prepare() -> None:
            try:
                if requested_dash_video_id:
                    try:
                        prepared_url = self._m3u8_ad_filter.prepare(
                            source_url,
                            current_item.headers,
                            dash_video_id=requested_dash_video_id,
                        )
                    except TypeError as exc:
                        if "dash_video_id" not in str(exc):
                            raise
                        prepared_url = self._m3u8_ad_filter.prepare(source_url, current_item.headers)
                else:
                    prepared_url = self._m3u8_ad_filter.prepare(source_url, current_item.headers)
            except Exception as exc:
                if self._is_window_alive():
                    self._playback_prepare_signals.failed.emit(request_id, str(exc))
                return
            if not self._is_window_alive():
                return
            self._playback_prepare_signals.succeeded.emit(request_id, prepared_url)

        self._enqueue_controller_task("播放地址预处理失败", prepare)
        return True

    def _playback_prepare_source_url(self, current_item: PlayItem) -> str:
        preferred_url = (current_item.original_url or current_item.url).strip()
        resolved_url = current_item.url.strip()
        if not current_item.parse_required or not preferred_url or not resolved_url or preferred_url == resolved_url:
            return preferred_url

        should_prepare = getattr(self._m3u8_ad_filter, "should_prepare", None)
        if callable(should_prepare):
            if not should_prepare(preferred_url) and should_prepare(resolved_url):
                return resolved_url
            return preferred_url
        if ".m3u8" not in preferred_url.lower() and ".m3u8" in resolved_url.lower():
            return resolved_url
        return preferred_url

    def _restore_current_index(self, previous_index: int) -> None:
        self.current_index = previous_index
        self.playlist.setCurrentRow(previous_index)
        self._refresh_window_title()
        self._refresh_parse_combo_enabled_state()

    def _restore_failed_spider_quality_switch(
        self,
        item: PlayItem,
        pending_prepare: _PendingPlaybackPrepare | None = None,
    ) -> bool:
        if pending_prepare is None or not pending_prepare.previous_url:
            return False
        item.url = pending_prepare.previous_url
        item.original_url = pending_prepare.previous_original_url
        item.selected_playback_quality_id = pending_prepare.previous_selected_playback_quality_id
        self._refresh_video_quality_state()
        return True

    def _restore_or_keep_current_index_after_failure(self, previous_index: int) -> None:
        if self._current_item_requires_parse():
            self.playlist.setCurrentRow(self.current_index)
            self._refresh_window_title()
            self._refresh_parse_combo_enabled_state()
            return
        self._restore_current_index(previous_index)

    def _requires_prepared_media_url(self, url: str) -> bool:
        return is_remote_iso_url(url)

    def _handle_play_item_resolve_succeeded(self, request_id: int, resolved_vod: VodItem | None) -> None:
        if request_id != self._play_item_request_id:
            return
        pending_load = self._pending_play_item_load
        self._pending_play_item_load = None
        if resolved_vod is not None:
            self._apply_resolved_vod(resolved_vod)
        if pending_load is None or not pending_load.wait_for_load:
            return
        if self.session is None or self.current_index != pending_load.index:
            return
        current_item = self.session.playlist[self.current_index]
        if not current_item.url:
            self._restore_current_index(pending_load.previous_index)
            self._append_log(f"播放失败: 没有可用的播放地址: {current_item.title}")
            return
        try:
            if self._start_playback_prepare(
                previous_index=pending_load.previous_index,
                start_position_seconds=pending_load.start_position_seconds,
                pause=pending_load.pause,
            ):
                return
            self._start_current_item_playback(
                start_position_seconds=pending_load.start_position_seconds,
                pause=pending_load.pause,
            )
        except Exception as exc:
            self._restore_current_index(pending_load.previous_index)
            self._append_log(f"播放失败: {exc}")

    def _handle_play_item_resolve_failed(self, request_id: int, message: str) -> None:
        if request_id != self._play_item_request_id:
            return
        pending_load = self._pending_play_item_load
        self._pending_play_item_load = None
        if pending_load is not None and pending_load.wait_for_load:
            self._restore_current_index(pending_load.previous_index)
            self._append_log(f"播放失败: {message}")
            return
        self._append_log(f"详情加载失败: {message}")

    def _handle_playback_loader_succeeded(self, request_id: int, load_result: PlaybackLoadResult | None) -> None:
        if request_id != self._playback_loader_request_id:
            return
        pending_loader = self._pending_playback_loader
        self._pending_playback_loader = None
        if pending_loader is None:
            return
        if self.session is None or self.current_index != pending_loader.index:
            return
        self._apply_playback_loader_result(load_result)
        self._render_poster()
        self._render_metadata()
        self._render_detail_fields()
        self._refresh_parse_combo_enabled_state()
        current_item = self.session.playlist[self.current_index]
        if not current_item.url:
            self._restore_or_keep_current_index_after_failure(pending_loader.previous_index)
            self._append_log(f"播放失败: 没有可用的播放地址: {current_item.title}")
            return
        try:
            if self._start_playback_prepare(
                previous_index=pending_loader.previous_index,
                start_position_seconds=pending_loader.start_position_seconds,
                pause=pending_loader.pause,
            ):
                return
            self._start_current_item_playback(
                start_position_seconds=pending_loader.start_position_seconds,
                pause=pending_loader.pause,
            )
        except Exception as exc:
            self._restore_or_keep_current_index_after_failure(pending_loader.previous_index)
            self._append_log(f"播放失败: {exc}")

    def _handle_playback_loader_failed(self, request_id: int, message: str) -> None:
        if request_id != self._playback_loader_request_id:
            return
        pending_loader = self._pending_playback_loader
        self._pending_playback_loader = None
        if pending_loader is None:
            return
        self._restore_or_keep_current_index_after_failure(pending_loader.previous_index)
        self._append_log(f"播放失败: {message}")

    def _handle_detail_action_succeeded(self, request_id: int, item: PlayItem, payload: object) -> None:
        if request_id != self._detail_action_request_id or self.session is None:
            return
        expected_index, actions = payload
        if expected_index != self.current_index:
            self._render_detail_actions()
            return
        if self.session.playlist[self.current_index] is not item:
            self._render_detail_actions()
            return
        item.detail_actions = list(actions) if isinstance(actions, list) else []
        self._render_detail_actions()

    def _handle_detail_action_failed(self, request_id: int, message: str) -> None:
        if request_id != self._detail_action_request_id:
            return
        self._append_log(message)
        self._render_detail_actions()

    def _handle_playback_prepare_succeeded(self, request_id: int, prepared_url: str) -> None:
        if request_id != self._playback_prepare_request_id:
            return
        pending_prepare = self._pending_playback_prepare
        self._pending_playback_prepare = None
        if pending_prepare is None:
            return
        if self.session is None or self.current_index != pending_prepare.index:
            return
        current_item = self.session.playlist[self.current_index]
        current_item.original_url = pending_prepare.source_url
        if pending_prepare.requested_dash_video_id:
            current_item.dash_video_id = pending_prepare.requested_dash_video_id
        current_item.url = prepared_url
        self._refresh_video_quality_state(prepared_url)
        try:
            self._start_current_item_playback(
                start_position_seconds=pending_prepare.start_position_seconds,
                pause=pending_prepare.pause,
            )
        except Exception as exc:
            self._restore_current_index(pending_prepare.previous_index)
            self._append_log(f"播放失败: {exc}")

    def _handle_playback_prepare_failed(self, request_id: int, message: str) -> None:
        if request_id != self._playback_prepare_request_id:
            return
        pending_prepare = self._pending_playback_prepare
        self._pending_playback_prepare = None
        if pending_prepare is None:
            return
        if self.session is None or self.current_index != pending_prepare.index:
            return
        current_item = self.session.playlist[self.current_index]
        if self._restore_failed_spider_quality_switch(current_item, pending_prepare):
            self._append_log(f"清晰度切换失败: {message}")
            return
        if self._requires_prepared_media_url(pending_prepare.source_url):
            self._append_log(f"播放失败: {message}")
            self._restore_current_index(pending_prepare.previous_index)
            return
        current_item.dash_video_id = pending_prepare.previous_dash_video_id
        self._refresh_video_quality_state(current_item.url)
        self._append_log(f"播放代理失败，继续播放原地址: {message}")
        try:
            self._start_current_item_playback(
                start_position_seconds=pending_prepare.start_position_seconds,
                pause=pending_prepare.pause,
            )
        except Exception as exc:
            self._restore_current_index(pending_prepare.previous_index)
            self._append_log(f"播放失败: {exc}")

    def _current_item_load_is_pending(self) -> bool:
        if self.session is None:
            return False
        pending_items = (
            self._pending_play_item_load,
            self._pending_playback_loader,
            self._pending_playback_prepare,
        )
        return any(
            pending is not None and pending.index == self.current_index
            for pending in pending_items
        )

    def _attempt_resume_seek(self, seconds: int, retries_remaining: int) -> None:
        if hasattr(self.video, "can_seek") and not self.video.can_seek():
            if retries_remaining > 0:
                QTimer.singleShot(
                    300,
                    lambda: self._attempt_resume_seek(seconds, retries_remaining=retries_remaining - 1),
                )
                return
            self._append_log("恢复播放失败: 媒体尚未进入可跳转状态")
            return
        try:
            self.video.seek(seconds)
        except Exception as exc:
            if retries_remaining > 0:
                QTimer.singleShot(
                    300,
                    lambda: self._attempt_resume_seek(seconds, retries_remaining=retries_remaining - 1),
                )
                return
            self._append_log(f"恢复播放失败: {exc}")

    def report_progress(self, force_remote_report: bool = False) -> None:
        if self.session is None:
            return
        if self._current_item_load_is_pending():
            return
        try:
            position_seconds = self.video.position_seconds()
            if position_seconds is None:
                return
            opening_seconds = self.opening_spin.value()
            ending_seconds = self.ending_spin.value()
            session = self.session
            current_index = self.current_index
            speed = self.current_speed
            paused = not self.is_playing
            session.opening_seconds = opening_seconds
            session.ending_seconds = ending_seconds

            def report() -> None:
                self.controller.report_progress(
                    session,
                    current_index=current_index,
                    position_seconds=position_seconds,
                    speed=speed,
                    opening_seconds=opening_seconds,
                    ending_seconds=ending_seconds,
                    paused=paused,
                    force_remote_report=force_remote_report,
                )

            self._enqueue_controller_task("进度上报失败", report)
        except Exception as exc:
            self._append_log(f"进度上报失败: {exc}")

    def _remember_restore_state(self) -> None:
        if self.session is None:
            return
        if hasattr(self.session, "start_index"):
            self.session.start_index = self.current_index
        if hasattr(self.session, "speed"):
            self.session.speed = self.current_speed
        if hasattr(self.session, "opening_seconds"):
            self.session.opening_seconds = self.opening_spin.value()
        if hasattr(self.session, "ending_seconds"):
            self.session.ending_seconds = self.ending_spin.value()
        try:
            position_seconds = self.video.position_seconds()
        except Exception:
            position_seconds = None
        if self._current_item_load_is_pending():
            position_seconds = None
        if position_seconds is not None and hasattr(self.session, "start_position_seconds"):
            self.session.start_position_seconds = position_seconds

    def _stop_current_playback(self) -> None:
        if self.session is None:
            return
        session = self.session
        current_index = self.current_index
        self._enqueue_controller_task(
            "停止上报失败",
            lambda: self.controller.stop_playback(session, current_index),
        )

    def _update_sidebar_visibility(self) -> None:
        self._apply_visibility_state()

    def _change_playlist_group(self, playlist_index: int) -> None:
        if self.session is None:
            return
        playlists = self._session_playlists()
        if not (0 <= playlist_index < len(playlists)):
            return
        if playlist_index == self.session.playlist_index:
            return
        target_playlist = playlists[playlist_index]
        if not target_playlist:
            return
        self.report_progress(force_remote_report=True)
        self._stop_current_playback()
        self._invalidate_play_item_resolution()
        self.session.playlist_index = playlist_index
        self.session.playlist = target_playlist
        self.current_index = min(self.current_index, len(target_playlist) - 1)
        self._render_playlist_group_combo()
        self._render_playlist_items()
        try:
            self._load_current_item(previous_index=self.current_index)
            self._refresh_window_title()
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")

    def _toggle_wide_mode(self) -> None:
        is_wide_mode = self.wide_button.isChecked()
        if self.config is not None and self.config.player_wide_mode != is_wide_mode:
            self.config.player_wide_mode = is_wide_mode
            self._save_config()
        if is_wide_mode:
            self._remember_sidebar_sizes()
            self._apply_visibility_state()
            self.main_splitter.setSizes([1, 0])
            return
        self._apply_visibility_state()
        if (
            self._restore_saved_splitter_on_next_wide_exit
            and self.config is not None
            and self.config.player_main_splitter_state
        ):
            self._restore_saved_splitter_on_next_wide_exit = False
            restored = self.main_splitter.restoreState(to_qbytearray(self.config.player_main_splitter_state))
            if restored and not self._has_collapsed_main_splitter_sizes():
                self._remember_sidebar_sizes()
                return
        self.main_splitter.setSizes(self._restoreable_sidebar_sizes())

    def _seek_relative(self, seconds: int) -> None:
        try:
            self.video.seek_relative(seconds)
        except Exception as exc:
            self._append_log(f"跳转失败: {exc}")

    def _replay_current_item(self) -> None:
        if self.session is None:
            return
        self.report_progress(force_remote_report=True)
        self._stop_current_playback()
        self.is_playing = True
        self._update_play_button_icon()
        self._refresh_window_title()
        self.playlist.setCurrentRow(self.current_index)
        self._load_current_item(
            start_position_seconds=0,
            preserve_primary_external_subtitle_selection=True,
        )

    def _toggle_mute(self) -> None:
        try:
            self.video.toggle_mute()
            self._is_muted = not self._is_muted
            self._update_mute_button_icon()
            if self.config is not None and self.config.player_muted != self._is_muted:
                self.config.player_muted = self._is_muted
                self._save_config()
        except Exception as exc:
            self._append_log(f"静音失败: {exc}")

    def _apply_muted_state(self) -> None:
        if not hasattr(self.video, "set_muted"):
            return
        try:
            self.video.set_muted(self._is_muted)
        except Exception as exc:
            self._append_log(f"静音恢复失败: {exc}")

    def _change_speed(self, text: str) -> None:
        try:
            self.current_speed = float(text.rstrip("x"))
            self.video.set_speed(self.current_speed)
        except Exception as exc:
            self._append_log(f"倍速设置失败: {exc}")

    def _change_opening_seconds(self, value: int) -> None:
        if self.session is None:
            return
        self.session.opening_seconds = value
        self.report_progress()

    def _change_ending_seconds(self, value: int) -> None:
        if self.session is None:
            return
        self.session.ending_seconds = value
        self.report_progress()

    def _reset_subtitle_combo(self) -> None:
        self.subtitle_combo.blockSignals(True)
        self.subtitle_combo.clear()
        self.subtitle_combo.addItem("字幕", ("auto", None))
        self.subtitle_combo.setCurrentIndex(0)
        self.subtitle_combo.setEnabled(False)
        self.subtitle_combo.blockSignals(False)

    def _reset_danmaku_combo(self, *, enabled: bool = False, current_index: int = 0) -> None:
        self.danmaku_combo.blockSignals(True)
        self.danmaku_combo.clear()
        labels = ["弹幕", "关闭", *(f"{line_count}行" for line_count in range(1, 11))]
        for label in labels:
            self.danmaku_combo.addItem(label)
        self.danmaku_combo.setCurrentIndex(current_index)
        self.danmaku_combo.setEnabled(enabled)
        self.danmaku_combo.blockSignals(False)

    def _reset_video_quality_combo(self) -> None:
        self.video_quality_combo.blockSignals(True)
        self.video_quality_combo.clear()
        self.video_quality_combo.addItem("清晰度", None)
        self.video_quality_combo.setCurrentIndex(0)
        self.video_quality_combo.setEnabled(False)
        self.video_quality_combo.blockSignals(False)

    def _reset_audio_combo(self) -> None:
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        self.audio_combo.addItem("音轨", ("auto", None))
        self.audio_combo.setCurrentIndex(0)
        self.audio_combo.setEnabled(False)
        self.audio_combo.blockSignals(False)

    def _populate_parse_combo(self) -> None:
        self.parse_combo.blockSignals(True)
        self.parse_combo.clear()
        self.parse_combo.addItem("解析", "")
        if self._playback_parser_service is not None:
            for parser in self._playback_parser_service.parsers():
                self.parse_combo.addItem(parser.label, parser.key)
        preferred_parse_key = "" if self.config is None else getattr(self.config, "preferred_parse_key", "")
        preferred_index = self.parse_combo.findData(preferred_parse_key)
        self.parse_combo.setCurrentIndex(preferred_index if preferred_index >= 0 else 0)
        self.parse_combo.setEnabled(False)
        self.parse_combo.blockSignals(False)

    def _change_parse_selection(self, index: int) -> None:
        if self.config is None:
            return
        parser_key = str(self.parse_combo.itemData(index) or "")
        if getattr(self.config, "preferred_parse_key", "") == parser_key:
            return
        self.config.preferred_parse_key = parser_key
        self._save_config()
        current_item = None
        if self.session is not None and 0 <= self.current_index < len(self.session.playlist):
            current_item = self.session.playlist[self.current_index]
        if (
            current_item is not None
            and self.session.playback_loader is not None
            and current_item.parse_required
        ):
            self._replay_current_item()

    def _preferred_danmaku_enabled(self) -> bool:
        if self.config is None:
            return True
        return bool(getattr(self.config, "preferred_danmaku_enabled", True))

    def _preferred_danmaku_line_count(self) -> int:
        if self.config is None:
            return 1
        return max(1, min(int(getattr(self.config, "preferred_danmaku_line_count", 1)), 10))

    def _preferred_danmaku_render_mode(self) -> str:
        if self.config is None:
            return "static"
        value = str(getattr(self.config, "preferred_danmaku_render_mode", "static") or "").strip()
        return value if value in {"static", "scroll_only", "mixed"} else "static"

    def _preferred_danmaku_color_mode(self) -> str:
        if self.config is None:
            return "source"
        value = str(getattr(self.config, "preferred_danmaku_color_mode", "source") or "").strip()
        return value if value in {"uniform", "source"} else "source"

    def _preferred_danmaku_uniform_color(self) -> str:
        if self.config is None:
            return "#FFFFFF"
        return self._normalize_danmaku_uniform_color(getattr(self.config, "preferred_danmaku_uniform_color", "#FFFFFF"))

    def _normalize_danmaku_uniform_color(self, value: object) -> str:
        normalized = str(value or "").strip().upper()
        if len(normalized) == 7 and normalized.startswith("#"):
            try:
                int(normalized[1:], 16)
            except ValueError:
                return "#FFFFFF"
            return normalized
        return "#FFFFFF"

    def _preferred_danmaku_position_preset(self) -> str:
        if self.config is None:
            return "top"
        value = str(getattr(self.config, "preferred_danmaku_position_preset", "top") or "").strip()
        return value if value in {"top", "upper", "mid_upper", "bottom"} else "top"

    def _preferred_danmaku_scroll_speed(self) -> float:
        if self.config is None:
            return 1.0
        try:
            value = float(getattr(self.config, "preferred_danmaku_scroll_speed", 1.0))
        except (TypeError, ValueError):
            return 1.0
        return max(0.5, min(round(value, 2), 2.0))

    def _preferred_danmaku_font_size(self) -> int:
        if self.config is None:
            return 32
        try:
            value = int(getattr(self.config, "preferred_danmaku_font_size", 32))
        except (TypeError, ValueError):
            return 32
        return max(16, min(value, 72))

    def _preferred_danmaku_combo_index(self) -> int:
        if not self._preferred_danmaku_enabled():
            return 1
        line_count = self._preferred_danmaku_line_count()
        return 0 if line_count == 1 else line_count + 1

    def _danmaku_line_count_from_combo_index(self, index: int) -> int:
        if index in (0, 1, 2):
            return 1
        return max(1, min(index - 1, 10))

    def _refresh_danmaku_combo_from_preferences(self) -> None:
        self._reset_danmaku_combo(enabled=self.danmaku_combo.isEnabled(), current_index=self._preferred_danmaku_combo_index())

    def _save_preferred_danmaku_selection(self, index: int) -> None:
        if self.config is None or index < 0:
            return
        enabled = index != 1
        line_count = self._danmaku_line_count_from_combo_index(index)
        if (
            self.config.preferred_danmaku_enabled == enabled
            and self.config.preferred_danmaku_line_count == line_count
        ):
            return
        self.config.preferred_danmaku_enabled = enabled
        self.config.preferred_danmaku_line_count = line_count
        self._save_config()

    def _save_danmaku_line_count(self, value: int) -> None:
        if self.config is None:
            return
        normalized = max(1, min(int(value), 10))
        if self.config.preferred_danmaku_line_count == normalized:
            return
        self.config.preferred_danmaku_line_count = normalized
        self._save_config()
        self._refresh_danmaku_combo_from_preferences()
        self._refresh_danmaku_settings_dialog_controls()
        self._reload_active_danmaku_for_render_settings()

    def _save_danmaku_render_mode(self, value: str) -> None:
        if self.config is None:
            return
        normalized = value if value in {"static", "scroll_only", "mixed"} else "static"
        if self.config.preferred_danmaku_render_mode == normalized:
            return
        self.config.preferred_danmaku_render_mode = normalized
        self._save_config()
        self._refresh_danmaku_settings_position_controls()
        self._reload_active_danmaku_for_render_settings()

    def _save_danmaku_color_mode(self, value: str) -> None:
        if self.config is None:
            return
        normalized = value if value in {"uniform", "source"} else "source"
        if self.config.preferred_danmaku_color_mode == normalized:
            return
        self.config.preferred_danmaku_color_mode = normalized
        self._save_config()
        self._refresh_danmaku_settings_color_controls()
        self._reload_active_danmaku_for_render_settings()

    def _save_danmaku_uniform_color(self, value: str) -> None:
        if self.config is None:
            return
        normalized = self._normalize_danmaku_uniform_color(value)
        if self.config.preferred_danmaku_uniform_color == normalized:
            return
        self.config.preferred_danmaku_uniform_color = normalized
        self._save_config()
        self._refresh_danmaku_settings_dialog_controls()
        self._reload_active_danmaku_for_render_settings()

    def _save_danmaku_position_preset(self, value: str) -> None:
        if self.config is None:
            return
        normalized = value if value in {"top", "upper", "mid_upper", "bottom"} else "top"
        if self.config.preferred_danmaku_position_preset == normalized:
            return
        self.config.preferred_danmaku_position_preset = normalized
        self._save_config()
        self._reload_active_danmaku_for_render_settings()

    def _save_danmaku_scroll_speed(self, value: float) -> None:
        if self.config is None:
            return
        normalized = max(0.5, min(round(float(value), 2), 2.0))
        if abs(float(getattr(self.config, "preferred_danmaku_scroll_speed", 1.0)) - normalized) < 0.001:
            return
        self.config.preferred_danmaku_scroll_speed = normalized
        self._save_config()
        self._refresh_danmaku_settings_dialog_controls()
        self._reload_active_danmaku_for_render_settings()

    def _save_danmaku_font_size(self, value: int) -> None:
        if self.config is None:
            return
        normalized = max(16, min(int(value), 72))
        if int(getattr(self.config, "preferred_danmaku_font_size", 32)) == normalized:
            return
        self.config.preferred_danmaku_font_size = normalized
        self._save_config()
        self._refresh_danmaku_settings_dialog_controls()
        self._reload_active_danmaku_for_render_settings()

    def _reload_active_danmaku_for_render_settings(self) -> None:
        if not self._preferred_danmaku_enabled():
            return
        if not self._danmaku_active:
            return
        if not self._current_play_item_danmaku_xml():
            return
        try:
            self._enable_danmaku(self._preferred_danmaku_line_count())
        except Exception as exc:
            self._append_log(f"弹幕设置应用失败: {exc}")

    def _current_item_requires_parse(self) -> bool:
        if self.session is None:
            return False
        if not (0 <= self.current_index < len(self.session.playlist)):
            return False
        return bool(getattr(self.session.playlist[self.current_index], "parse_required", False))

    def _refresh_parse_combo_enabled_state(self) -> None:
        self.parse_combo.setEnabled(self._current_item_requires_parse())

    def _mark_manual_subtitle_switch_refresh(self) -> None:
        self._manual_subtitle_switch_refresh_until = (
            time.monotonic() + self._MANUAL_SUBTITLE_SWITCH_REFRESH_WINDOW_SECONDS
        )
        self._skip_audio_refresh_for_manual_subtitle_switch = True

    def _clear_manual_subtitle_switch_refresh(self) -> None:
        self._manual_subtitle_switch_refresh_until = 0.0
        self._skip_audio_refresh_for_manual_subtitle_switch = False

    def _manual_subtitle_switch_refresh_active(self) -> bool:
        if self._manual_subtitle_switch_refresh_until <= 0:
            return False
        if time.monotonic() > self._manual_subtitle_switch_refresh_until:
            self._clear_manual_subtitle_switch_refresh()
            return False
        return True

    def _remember_track_preference(self, track: SubtitleTrack) -> None:
        self._subtitle_preference = SubtitlePreference(
            mode="track",
            title=track.title,
            lang=track.lang,
            is_default=track.is_default,
            is_forced=track.is_forced,
        )

    def _current_item_external_subtitles(self) -> list[ExternalSubtitleOption]:
        current_item = self._current_play_item()
        if current_item is None:
            return []
        return list(current_item.external_subtitles)

    def _current_item_secondary_external_subtitles(self) -> list[ExternalSubtitleOption]:
        return [subtitle for subtitle in self._current_item_external_subtitles() if subtitle.source != "spider"]

    def _current_item_auto_spider_external_subtitles(self) -> list[ExternalSubtitleOption]:
        return [subtitle for subtitle in self._current_item_external_subtitles() if subtitle.source == "spider"]

    def _find_current_item_external_subtitle(self, url: str) -> ExternalSubtitleOption | None:
        return next((subtitle for subtitle in self._current_item_external_subtitles() if subtitle.url == url), None)

    def _match_current_item_external_subtitle(
        self,
        selection: ExternalSubtitleSelection | None,
    ) -> ExternalSubtitleOption | None:
        if selection is None:
            return None
        exact_match = self._find_current_item_external_subtitle(selection.option_url)
        if exact_match is not None:
            return exact_match
        if not selection.option_name:
            return None
        candidates = [
            subtitle
            for subtitle in self._current_item_external_subtitles()
            if subtitle.source == selection.source and subtitle.name == selection.option_name
        ]
        if not candidates:
            return None
        ranked_candidates = sorted(
            candidates,
            key=lambda subtitle: (
                int(bool(selection.option_lang) and subtitle.lang == selection.option_lang),
                int(bool(selection.option_format) and subtitle.format == selection.option_format),
            ),
            reverse=True,
        )
        return ranked_candidates[0]

    def _current_primary_external_subtitle(self) -> ExternalSubtitleOption | None:
        return self._match_current_item_external_subtitle(self._primary_external_subtitle_selection)

    def _current_auto_spider_subtitle_attempt_key(self, subtitle: ExternalSubtitleOption) -> tuple[int, str]:
        current_item = self._current_play_item()
        return (id(current_item), subtitle.url)

    def _should_recheck_subtitle_tracks_after_stale_snapshot(self) -> bool:
        current_external_subtitle = self._current_primary_external_subtitle()
        if current_external_subtitle is None or self._primary_external_subtitle_track_id is not None:
            return False
        if self._subtitle_preference.mode == "external":
            return True
        return self._subtitle_preference.mode == "auto" and current_external_subtitle.source == "spider"

    def _should_retry_followup_subtitle_refresh(self, current_item: PlayItem) -> bool:
        if self.session is None:
            return False
        if self.current_index >= len(self.session.playlist):
            return False
        if self.session.playlist[self.current_index] is not current_item:
            return False
        if self._primary_external_subtitle_track_id is not None:
            return False
        current_external_subtitle = self._current_primary_external_subtitle()
        if current_external_subtitle is not None:
            if self._subtitle_preference.mode == "external":
                return True
            return self._subtitle_preference.mode == "auto" and current_external_subtitle.source == "spider"
        return self._should_auto_apply_spider_subtitle()

    def _remove_external_subtitle_track(self, track_id: int | None) -> None:
        if track_id is None or not hasattr(self.video, "remove_subtitle_track"):
            return
        self.video.remove_subtitle_track(track_id)

    def _clear_primary_external_subtitle(self, *, preserve_selection: bool = False) -> None:
        self._stop_primary_external_subtitle_retry()
        self._remove_external_subtitle_track(self._primary_external_subtitle_track_id)
        if not preserve_selection:
            self._primary_external_subtitle_selection = None
        self._primary_external_subtitle_track_id = None
        self._primary_external_subtitle_path = None

    def _clear_secondary_external_subtitle(self, *, preserve_selection: bool = False) -> None:
        self._remove_external_subtitle_track(self._secondary_external_subtitle_track_id)
        if not preserve_selection:
            self._secondary_external_subtitle_selection = None
        self._secondary_external_subtitle_track_id = None
        self._secondary_external_subtitle_path = None

    def _clear_external_subtitle_tracks(
        self,
        *,
        preserve_primary_selection: bool = False,
        preserve_secondary_selection: bool = False,
    ) -> None:
        self._clear_primary_external_subtitle(preserve_selection=preserve_primary_selection)
        self._clear_secondary_external_subtitle(preserve_selection=preserve_secondary_selection)

    def _reload_selected_primary_external_subtitle_if_needed(self) -> bool:
        current_external_subtitle = self._current_primary_external_subtitle()
        if current_external_subtitle is None or self._primary_external_subtitle_track_id is not None:
            return False
        if self._subtitle_preference.mode == "external":
            pass
        elif self._subtitle_preference.mode == "auto" and current_external_subtitle.source == "spider":
            pass
        else:
            return False
        if not self._ensure_primary_external_subtitle_loaded(current_external_subtitle):
            return True
        if not self._apply_primary_external_subtitle_track(self._primary_external_subtitle_track_id):
            return True
        self._sync_subtitle_combo_without_tracks()
        return True

    def _primary_external_subtitle_track_needs_reapply(self) -> bool:
        current_external_subtitle = self._current_primary_external_subtitle()
        track_id = self._primary_external_subtitle_track_id
        if current_external_subtitle is None or track_id is None:
            return False
        getter = getattr(self.video, "current_subtitle_track_id", None)
        if not callable(getter):
            return False
        current_track_id = getter()
        if current_track_id == track_id:
            return False
        return True

    def _sync_subtitle_combo_without_tracks(self) -> None:
        self.subtitle_combo.blockSignals(True)
        try:
            current_external_subtitle = self._current_primary_external_subtitle()
            if current_external_subtitle is not None:
                for index in range(self.subtitle_combo.count()):
                    item_data = self.subtitle_combo.itemData(index)
                    if (
                        isinstance(item_data, tuple)
                        and len(item_data) == 3
                        and item_data[0] == "external"
                        and getattr(item_data[2], "url", None) == current_external_subtitle.url
                    ):
                        self.subtitle_combo.setCurrentIndex(index)
                        return
            if self._subtitle_preference.mode == "off":
                self.subtitle_combo.setCurrentIndex(1 if self.subtitle_combo.count() > 1 else 0)
                return
            self.subtitle_combo.setCurrentIndex(0)
        finally:
            self.subtitle_combo.blockSignals(False)

    def _sync_subtitle_combo_for_current_state(self) -> None:
        if self._subtitle_tracks:
            self._sync_subtitle_combo_to_preference()
            return
        self._sync_subtitle_combo_without_tracks()

    def _should_auto_apply_spider_subtitle(self) -> bool:
        if self._auto_spider_subtitle_suppressed:
            return False
        if self._subtitle_preference.mode != "auto":
            return False
        if self._subtitle_tracks:
            return False
        if self._primary_external_subtitle_track_id is not None and self._current_primary_external_subtitle() is not None:
            return False
        return bool(self._current_item_auto_spider_external_subtitles())

    def _stop_primary_external_subtitle_retry(self) -> None:
        self._primary_external_subtitle_retry_timer.stop()
        self._primary_external_subtitle_retry_attempts = 0

    def _schedule_primary_external_subtitle_retry_for_pending_track(self) -> bool:
        if self._primary_external_subtitle_retry_attempts >= 3:
            self._stop_primary_external_subtitle_retry()
            return False
        if not self._primary_external_subtitle_retry_timer.isActive():
            self._primary_external_subtitle_retry_attempts += 1
            self._primary_external_subtitle_retry_timer.start(400)
        return True

    def _should_retry_primary_external_subtitle_apply(self, exc: Exception) -> bool:
        if self._primary_external_subtitle_retry_attempts >= 3:
            return False
        return self._is_mpv_command_error(exc)

    def _schedule_primary_external_subtitle_retry(self) -> None:
        self._primary_external_subtitle_retry_attempts += 1
        self._primary_external_subtitle_retry_timer.start(400)

    def _ensure_primary_external_subtitle_loaded(self, subtitle: ExternalSubtitleOption) -> bool:
        if self._primary_external_subtitle_track_id is not None:
            return True
        try:
            loaded_track_id, subtitle_path = self._load_external_subtitle(subtitle, secondary=False)
        except Exception as exc:
            if self._should_retry_primary_external_subtitle_apply(exc):
                self._schedule_primary_external_subtitle_retry()
                return False
            self._stop_primary_external_subtitle_retry()
            raise
        self._primary_external_subtitle_track_id = loaded_track_id
        self._primary_external_subtitle_path = subtitle_path
        if loaded_track_id is None:
            self._schedule_primary_external_subtitle_retry_for_pending_track()
            return False
        return True

    def _apply_primary_external_subtitle_track(self, track_id: int | None) -> bool:
        if track_id is None:
            self._schedule_primary_external_subtitle_retry_for_pending_track()
            return False
        try:
            self.video.apply_subtitle_mode("track", track_id=track_id)
        except Exception as exc:
            if self._should_retry_primary_external_subtitle_apply(exc):
                self._schedule_primary_external_subtitle_retry()
                return False
            self._stop_primary_external_subtitle_retry()
            raise
        self._stop_primary_external_subtitle_retry()
        return True

    def _retry_apply_primary_external_subtitle(self) -> None:
        current_external_subtitle = self._current_primary_external_subtitle()
        if current_external_subtitle is None:
            self._stop_primary_external_subtitle_retry()
            return
        try:
            if not self._ensure_primary_external_subtitle_loaded(current_external_subtitle):
                return
            track_id = self._primary_external_subtitle_track_id
            if track_id is None:
                self._schedule_primary_external_subtitle_retry_for_pending_track()
                return
            if not self._apply_primary_external_subtitle_track(track_id):
                return
        except Exception as exc:
            self._append_log(f"字幕切换失败: {exc}")
            self._clear_primary_external_subtitle()
            self._sync_subtitle_combo_for_current_state()
            return
        self._sync_subtitle_combo_for_current_state()

    def _auto_apply_spider_subtitle_if_needed(self) -> bool:
        if not self._should_auto_apply_spider_subtitle():
            return False
        subtitle = self._current_item_auto_spider_external_subtitles()[0]
        attempt_key = self._current_auto_spider_subtitle_attempt_key(subtitle)
        if self._auto_spider_subtitle_attempted_key == attempt_key:
            return False
        self._auto_spider_subtitle_attempted_key = attempt_key
        self._primary_external_subtitle_selection = ExternalSubtitleSelection(
            source=subtitle.source,
            option_url=subtitle.url,
            option_name=subtitle.name,
            option_lang=subtitle.lang,
            option_format=subtitle.format,
        )
        if not self._ensure_primary_external_subtitle_loaded(subtitle):
            return True
        if not self._apply_primary_external_subtitle_track(self._primary_external_subtitle_track_id):
            return True
        self._sync_subtitle_combo_without_tracks()
        return True

    def _suppress_auto_spider_subtitle_for_current_item(self) -> None:
        self._auto_spider_subtitle_suppressed = True

    def _build_primary_subtitle_options(self, tracks: list[SubtitleTrack]) -> list[UnifiedSubtitleOption]:
        options: list[UnifiedSubtitleOption] = []
        for track in tracks:
            options.append(UnifiedSubtitleOption(label=track.label, mode="track", track_id=track.id))
        for subtitle in self._current_item_external_subtitles():
            options.append(
                UnifiedSubtitleOption(
                    label=subtitle.name,
                    mode="external",
                    external_subtitle=subtitle,
                )
            )
        return options

    def _populate_subtitle_combo(self, tracks: list[SubtitleTrack]) -> None:
        self._unified_primary_subtitle_options = self._build_primary_subtitle_options(tracks)
        self.subtitle_combo.blockSignals(True)
        self.subtitle_combo.clear()
        self.subtitle_combo.addItem("字幕", ("auto", None))
        if self._unified_primary_subtitle_options:
            self.subtitle_combo.addItem("关闭字幕", ("off", None))
            for option in self._unified_primary_subtitle_options:
                self.subtitle_combo.addItem(
                    option.label,
                    (option.mode, option.track_id, option.external_subtitle),
                )
        self.subtitle_combo.setEnabled(bool(self._unified_primary_subtitle_options))
        self.subtitle_combo.setCurrentIndex(0)
        self.subtitle_combo.blockSignals(False)

    def _populate_audio_combo(self, tracks: list[AudioTrack]) -> None:
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        self.audio_combo.addItem("音轨", ("auto", None))
        if len(tracks) > 1:
            for track in tracks:
                self.audio_combo.addItem(track.label, ("track", track.id))
        self.audio_combo.setEnabled(len(tracks) > 1)
        self.audio_combo.setCurrentIndex(0)
        self.audio_combo.blockSignals(False)

    def _populate_video_quality_combo(
        self,
        qualities: list[VideoQualityOption],
        selected_quality_id: str | None,
    ) -> None:
        self.video_quality_combo.blockSignals(True)
        self.video_quality_combo.clear()
        if not qualities:
            self.video_quality_combo.addItem("清晰度", None)
            self.video_quality_combo.setCurrentIndex(0)
            self.video_quality_combo.setEnabled(False)
            self.video_quality_combo.blockSignals(False)
            return
        selected_index = 0
        for index, quality in enumerate(qualities):
            self.video_quality_combo.addItem(quality.label, quality.id)
            if quality.id == selected_quality_id:
                selected_index = index
        self.video_quality_combo.setCurrentIndex(selected_index)
        self.video_quality_combo.setEnabled(len(qualities) > 1)
        self.video_quality_combo.blockSignals(False)

    def _remember_audio_track_preference(self, track: AudioTrack) -> None:
        self._audio_preference = AudioPreference(
            mode="track",
            title=track.title,
            lang=track.lang,
            is_default=track.is_default,
            is_forced=track.is_forced,
        )

    def _audio_track_match_score(self, track: AudioTrack, preference: AudioPreference) -> tuple[int, int, int]:
        return (
            int(bool(preference.title) and track.title == preference.title),
            int(bool(preference.lang) and track.lang == preference.lang),
            int(track.is_forced == preference.is_forced and track.is_default == preference.is_default),
        )

    def _matching_audio_track_for_preference(self) -> AudioTrack | None:
        if self._audio_preference.mode != "track" or len(self._audio_tracks) <= 1:
            return None
        ranked_tracks = sorted(
            self._audio_tracks,
            key=lambda track: self._audio_track_match_score(track, self._audio_preference),
            reverse=True,
        )
        best_track = ranked_tracks[0]
        if self._audio_track_match_score(best_track, self._audio_preference) == (0, 0, 0):
            return None
        return best_track

    def _apply_audio_preference(self) -> None:
        self.audio_combo.blockSignals(True)
        try:
            if self._audio_preference.mode == "track":
                matched_track = self._matching_audio_track_for_preference()
                if matched_track is not None:
                    applied_track_id = self.video.apply_audio_mode("track", track_id=matched_track.id)
                    for index, track in enumerate(self._audio_tracks, start=1):
                        if track.id == applied_track_id:
                            self.audio_combo.setCurrentIndex(index)
                            return
                self._audio_preference = AudioPreference()

            self.video.apply_audio_mode("auto")
            self.audio_combo.setCurrentIndex(0)
        finally:
            self.audio_combo.blockSignals(False)

    def _apply_subtitle_preference(self) -> None:
        self.subtitle_combo.blockSignals(True)
        try:
            current_external_subtitle = self._current_primary_external_subtitle()
            if current_external_subtitle is not None:
                if self._primary_external_subtitle_track_id is not None:
                    if not self._apply_primary_external_subtitle_track(self._primary_external_subtitle_track_id):
                        self._sync_subtitle_combo_for_current_state()
                        return
                    for index in range(self.subtitle_combo.count()):
                        item_data = self.subtitle_combo.itemData(index)
                        if (
                            isinstance(item_data, tuple)
                            and len(item_data) == 3
                            and item_data[0] == "external"
                            and getattr(item_data[2], "url", None) == current_external_subtitle.url
                        ):
                            self.subtitle_combo.setCurrentIndex(index)
                            return
                    self._clear_primary_external_subtitle()
                elif self._subtitle_preference.mode == "external" or (
                    self._subtitle_preference.mode == "auto" and current_external_subtitle.source == "spider"
                ):
                    self._sync_subtitle_combo_to_preference()
                    return
            elif self._subtitle_preference.mode == "external":
                self._subtitle_preference = SubtitlePreference()

            if self._subtitle_preference.mode == "off":
                self.video.apply_subtitle_mode("off")
                if self.subtitle_combo.count() > 1:
                    self.subtitle_combo.setCurrentIndex(1)
                return

            if self._subtitle_preference.mode == "track":
                matched_track = self._matching_track_for_preference()
                if matched_track is not None:
                    applied_track_id = self.video.apply_subtitle_mode("track", track_id=matched_track.id)
                    for index, track in enumerate(self._subtitle_tracks, start=2):
                        if track.id == applied_track_id:
                            self.subtitle_combo.setCurrentIndex(index)
                            return
                self._subtitle_preference = SubtitlePreference()

            self.video.apply_subtitle_mode("auto")
            self.subtitle_combo.setCurrentIndex(0)
        finally:
            self.subtitle_combo.blockSignals(False)

    def _sync_subtitle_combo_to_preference(self) -> None:
        self.subtitle_combo.blockSignals(True)
        try:
            current_external_subtitle = self._current_primary_external_subtitle()
            if current_external_subtitle is not None:
                for index in range(self.subtitle_combo.count()):
                    item_data = self.subtitle_combo.itemData(index)
                    if (
                        isinstance(item_data, tuple)
                        and len(item_data) == 3
                        and item_data[0] == "external"
                        and getattr(item_data[2], "url", None) == current_external_subtitle.url
                    ):
                        self.subtitle_combo.setCurrentIndex(index)
                        return
                self._clear_primary_external_subtitle()
            elif self._subtitle_preference.mode == "external":
                self._subtitle_preference = SubtitlePreference()
            if self._subtitle_preference.mode == "off":
                self.subtitle_combo.setCurrentIndex(1 if self.subtitle_combo.count() > 1 else 0)
                return
            if self._subtitle_preference.mode == "track":
                matched_track = self._matching_track_for_preference()
                if matched_track is not None:
                    for index, track in enumerate(self._subtitle_tracks, start=2):
                        if track.id == matched_track.id:
                            self.subtitle_combo.setCurrentIndex(index)
                            return
                self._subtitle_preference = SubtitlePreference()
            self.subtitle_combo.setCurrentIndex(0)
        finally:
            self.subtitle_combo.blockSignals(False)

    def _subtitle_track_match_score(self, track: SubtitleTrack, preference: SubtitlePreference) -> tuple[int, int, int]:
        return (
            int(bool(preference.title) and track.title == preference.title),
            int(bool(preference.lang) and track.lang == preference.lang),
            int(track.is_forced == preference.is_forced and track.is_default == preference.is_default),
        )

    def _matching_track_for_preference(self) -> SubtitleTrack | None:
        if self._subtitle_preference.mode != "track" or not self._subtitle_tracks:
            return None
        ranked_tracks = sorted(
            self._subtitle_tracks,
            key=lambda track: self._subtitle_track_match_score(track, self._subtitle_preference),
            reverse=True,
        )
        best_track = ranked_tracks[0]
        if self._subtitle_track_match_score(best_track, self._subtitle_preference) == (0, 0, 0):
            return None
        return best_track

    def _secondary_subtitle_track_match_score(
        self,
        track: SubtitleTrack,
        preference: SecondarySubtitlePreference,
    ) -> tuple[int, int, int]:
        return (
            int(bool(preference.title) and track.title == preference.title),
            int(bool(preference.lang) and track.lang == preference.lang),
            int(track.is_forced == preference.is_forced and track.is_default == preference.is_default),
        )

    def _matching_secondary_track_for_preference(self) -> SubtitleTrack | None:
        if self._secondary_subtitle_preference.mode != "track" or not self._subtitle_tracks:
            return None
        ranked_tracks = sorted(
            self._subtitle_tracks,
            key=lambda track: self._secondary_subtitle_track_match_score(track, self._secondary_subtitle_preference),
            reverse=True,
        )
        best_track = ranked_tracks[0]
        if self._secondary_subtitle_track_match_score(best_track, self._secondary_subtitle_preference) == (0, 0, 0):
            return None
        return best_track

    def _apply_secondary_subtitle_preference(self) -> None:
        if self._secondary_external_subtitle_selection is not None and self._secondary_external_subtitle_track_id is not None:
            subtitle = self._match_current_item_external_subtitle(self._secondary_external_subtitle_selection)
            if subtitle is not None:
                self.video.apply_secondary_subtitle_mode("track", track_id=self._secondary_external_subtitle_track_id)
                return
            self._clear_secondary_external_subtitle()
        elif self._secondary_subtitle_preference.mode == "external":
            self._secondary_subtitle_preference = SecondarySubtitlePreference()
        if self._secondary_subtitle_preference.mode == "off":
            self.video.apply_secondary_subtitle_mode("off")
            return
        matched_track = self._matching_secondary_track_for_preference()
        if matched_track is None:
            self._secondary_subtitle_preference = SecondarySubtitlePreference()
            self.video.apply_secondary_subtitle_mode("off")
            return
        self.video.apply_secondary_subtitle_mode("track", track_id=matched_track.id)

    def _current_play_item_danmaku_xml(self) -> str:
        if self.session is None or not self.session.playlist:
            return ""
        return self.session.playlist[self.current_index].danmaku_xml

    def _cleanup_danmaku_temp_file(self) -> None:
        self._danmaku_temp_path = None

    def _restore_secondary_subtitle_position_after_danmaku(self) -> None:
        if self._danmaku_restore_secondary_position is None:
            return
        if (
            not hasattr(self.video, "set_secondary_subtitle_position")
            or not getattr(
                self.video,
                "supports_secondary_subtitle_position",
                lambda: False,
            )()
        ):
            self._danmaku_restore_secondary_position = None
            return
        try:
            self.video.set_secondary_subtitle_position(self._danmaku_restore_secondary_position)
        except Exception as exc:
            self._append_log(f"次字幕位置恢复失败: {exc}")
        finally:
            self._danmaku_restore_secondary_position = None

    def _restore_secondary_subtitle_scale_after_danmaku(self) -> None:
        if self._danmaku_restore_secondary_scale is None:
            return
        if (
            not hasattr(self.video, "set_secondary_subtitle_scale")
            or not getattr(
                self.video,
                "supports_secondary_subtitle_scale",
                lambda: False,
            )()
        ):
            self._danmaku_restore_secondary_scale = None
            return
        try:
            self.video.set_secondary_subtitle_scale(self._danmaku_restore_secondary_scale)
        except Exception as exc:
            self._append_log(f"次字幕大小恢复失败: {exc}")
        finally:
            self._danmaku_restore_secondary_scale = None

    def _restore_main_subtitle_scale_after_danmaku(self) -> None:
        if self._danmaku_restore_main_scale is None:
            return
        if (
            not hasattr(self.video, "set_subtitle_scale")
            or not getattr(
                self.video,
                "supports_subtitle_scale",
                lambda: False,
            )()
        ):
            self._danmaku_restore_main_scale = None
            return
        try:
            self.video.set_subtitle_scale(self._danmaku_restore_main_scale)
        except Exception as exc:
            self._append_log(f"主字幕大小恢复失败: {exc}")
        finally:
            self._danmaku_restore_main_scale = None

    def _restore_main_subtitle_ass_override_after_danmaku(self) -> None:
        if self._danmaku_restore_main_ass_override is None:
            return
        if (
            not hasattr(self.video, "set_subtitle_ass_override")
            or not getattr(self.video, "supports_subtitle_ass_override", lambda: False)()
        ):
            self._danmaku_restore_main_ass_override = None
            return
        try:
            self.video.set_subtitle_ass_override(self._danmaku_restore_main_ass_override)
        except Exception as exc:
            self._append_log(f"主字幕样式恢复失败: {exc}")
        finally:
            self._danmaku_restore_main_ass_override = None

    def _restore_secondary_subtitle_ass_override_after_danmaku(self) -> None:
        if self._danmaku_restore_secondary_ass_override is None:
            return
        if (
            not hasattr(self.video, "set_secondary_subtitle_ass_override")
            or not getattr(self.video, "supports_secondary_subtitle_ass_override", lambda: False)()
        ):
            self._danmaku_restore_secondary_ass_override = None
            return
        try:
            self.video.set_secondary_subtitle_ass_override(self._danmaku_restore_secondary_ass_override)
        except Exception as exc:
            self._append_log(f"次字幕样式恢复失败: {exc}")
        finally:
            self._danmaku_restore_secondary_ass_override = None

    def _restore_subtitle_ass_force_margins_after_danmaku(self) -> None:
        if self._danmaku_restore_ass_force_margins is None:
            return
        if (
            not hasattr(self.video, "set_subtitle_ass_force_margins")
            or not getattr(self.video, "supports_subtitle_ass_force_margins", lambda: False)()
        ):
            self._danmaku_restore_ass_force_margins = None
            return
        try:
            self.video.set_subtitle_ass_force_margins(self._danmaku_restore_ass_force_margins)
        except Exception as exc:
            self._append_log(f"黑边字幕恢复失败: {exc}")
        finally:
            self._danmaku_restore_ass_force_margins = None

    def _clear_active_danmaku(self, *, restore_position: bool = True) -> None:
        self._danmaku_retry_timer.stop()
        self._pending_danmaku_timer.stop()
        self._danmaku_retry_attempts = 0
        if self._danmaku_track_id is not None and hasattr(self.video, "remove_subtitle_track"):
            try:
                self.video.remove_subtitle_track(self._danmaku_track_id)
            except Exception as exc:
                self._append_log(f"弹幕关闭失败: {exc}")
        self._danmaku_track_id = None
        self._danmaku_active = False
        if restore_position:
            self._restore_secondary_subtitle_position_after_danmaku()
            self._restore_secondary_subtitle_scale_after_danmaku()
            self._restore_main_subtitle_scale_after_danmaku()
            self._restore_secondary_subtitle_ass_override_after_danmaku()
            self._restore_main_subtitle_ass_override_after_danmaku()
            self._restore_subtitle_ass_force_margins_after_danmaku()
        self._danmaku_loading_slot = None
        self._danmaku_uses_secondary_slot = None
        self._cleanup_danmaku_temp_file()

    def _write_danmaku_subtitle_file(self, xml_text: str, line_count: int) -> Path | None:
        self._cleanup_danmaku_temp_file()
        temp_path = load_or_create_danmaku_ass_cache(
            xml_text,
            line_count,
            render_mode=self._preferred_danmaku_render_mode(),
            color_mode=self._preferred_danmaku_color_mode(),
            uniform_color=self._preferred_danmaku_uniform_color(),
            position_preset=self._preferred_danmaku_position_preset(),
            scroll_speed=self._preferred_danmaku_scroll_speed(),
            font_size=self._preferred_danmaku_font_size(),
        )
        if temp_path is None:
            return None
        self._danmaku_temp_path = temp_path
        return temp_path

    def _apply_danmaku_secondary_scale(self) -> None:
        if (
            not hasattr(self.video, "set_secondary_subtitle_scale")
            or not getattr(
                self.video,
                "supports_secondary_subtitle_scale",
                lambda: False,
            )()
        ):
            return
        try:
            self.video.set_secondary_subtitle_scale(self._DANMAKU_SECONDARY_SCALE)
        except Exception as exc:
            self._append_log(f"弹幕大小设置失败: {exc}")

    def _apply_danmaku_main_scale(self) -> None:
        if (
            not hasattr(self.video, "set_subtitle_scale")
            or not getattr(
                self.video,
                "supports_subtitle_scale",
                lambda: False,
            )()
        ):
            return
        try:
            self.video.set_subtitle_scale(self._DANMAKU_SECONDARY_SCALE)
        except Exception as exc:
            self._append_log(f"弹幕大小设置失败: {exc}")

    def _apply_danmaku_scale(self) -> None:
        if self._danmaku_uses_secondary_slot is False:
            self._apply_danmaku_main_scale()
            return
        self._apply_danmaku_secondary_scale()

    def _enable_danmaku(self, line_count: int) -> None:
        xml_text = self._current_play_item_danmaku_xml()
        if not xml_text:
            return
        if self._danmaku_restore_secondary_position is None:
            self._danmaku_restore_secondary_position = self._secondary_subtitle_position
        if (
            self._danmaku_restore_ass_force_margins is None
            and hasattr(self.video, "subtitle_ass_force_margins")
            and getattr(self.video, "supports_subtitle_ass_force_margins", lambda: False)()
        ):
            self._danmaku_restore_ass_force_margins = self.video.subtitle_ass_force_margins()
        if (
            hasattr(self.video, "set_subtitle_ass_force_margins")
            and getattr(self.video, "supports_subtitle_ass_force_margins", lambda: False)()
        ):
            self.video.set_subtitle_ass_force_margins("yes")
        self._clear_active_danmaku(restore_position=False)
        subtitle_path = self._write_danmaku_subtitle_file(xml_text, line_count)
        if subtitle_path is None:
            raise ValueError("弹幕为空")
        if not hasattr(self.video, "load_external_subtitle"):
            raise RuntimeError("播放器不支持外挂弹幕")
        track_id = self._load_primary_danmaku_subtitle(subtitle_path)
        if track_id is None:
            raise RuntimeError("播放器未返回弹幕轨道")
        self._danmaku_track_id = track_id
        self._danmaku_active = True
        self._danmaku_line_count = line_count

    def _load_primary_danmaku_subtitle(self, subtitle_path: Path) -> int | None:
        self._danmaku_loading_slot = "primary"
        try:
            track_id = self.video.load_external_subtitle(str(subtitle_path), select_for_secondary=False)
            if track_id is not None and hasattr(self.video, "apply_subtitle_mode"):
                self.video.apply_subtitle_mode("track", track_id=track_id)
        finally:
            self._danmaku_loading_slot = None
        self._danmaku_uses_secondary_slot = False
        return track_id

    def _configure_danmaku_for_current_item(self) -> None:
        self._danmaku_retry_timer.stop()
        xml_text = self._current_play_item_danmaku_xml()
        if not xml_text:
            if self.session is not None and self.session.playlist[self.current_index].danmaku_pending:
                self._reset_danmaku_combo()
                if not self._pending_danmaku_timer.isActive():
                    self._pending_danmaku_timer.start()
                return
            self._pending_danmaku_timer.stop()
            self._reset_danmaku_combo()
            self._danmaku_retry_attempts = 0
            return
        self._pending_danmaku_timer.stop()
        preferred_index = self._preferred_danmaku_combo_index()
        self._reset_danmaku_combo(enabled=True, current_index=preferred_index)
        if preferred_index == 1:
            self._clear_active_danmaku()
            self._danmaku_retry_attempts = 0
            return
        try:
            self._enable_danmaku(self._preferred_danmaku_line_count())
            self._reset_danmaku_combo(enabled=True, current_index=preferred_index)
            self._danmaku_retry_attempts = 0
        except Exception as exc:
            if self._should_retry_danmaku_load(exc):
                self._schedule_danmaku_retry()
                return
            self._append_log(f"弹幕加载失败: {exc}")
            self._clear_active_danmaku()
            self._reset_danmaku_combo(enabled=True, current_index=1)

    def _should_retry_danmaku_load(self, exc: Exception) -> bool:
        if self._danmaku_retry_attempts >= 3:
            return False
        return self._is_mpv_command_error(exc)

    def _is_mpv_command_error(self, exc: Exception) -> bool:
        return "Error running mpv command" in str(exc)

    def _schedule_danmaku_retry(self) -> None:
        self._danmaku_retry_attempts += 1
        self._danmaku_retry_timer.start(400)

    def _retry_configure_danmaku_for_current_item(self) -> None:
        if self.session is None:
            return
        if not self._current_play_item_danmaku_xml():
            self._danmaku_retry_attempts = 0
            return
        self._configure_danmaku_for_current_item()

    def _refresh_pending_danmaku_for_current_item(self) -> None:
        if self.session is None:
            self._pending_danmaku_timer.stop()
            return
        current_item = self.session.playlist[self.current_index]
        self._refresh_danmaku_source_dialog_actions(current_item)
        if current_item.danmaku_xml:
            self._pending_danmaku_timer.stop()
            self._configure_danmaku_for_current_item()
            return
        if not current_item.danmaku_pending:
            self._pending_danmaku_timer.stop()

    def _refresh_subtitle_state(self) -> None:
        if not hasattr(self.video, "subtitle_tracks") or not hasattr(self.video, "apply_subtitle_mode"):
            self._subtitle_tracks = []
            self._subtitle_preference = SubtitlePreference()
            self._reset_subtitle_combo()
            return
        manual_switch_refresh = self._manual_subtitle_switch_refresh_active()
        remembered_main_subtitle_scale = self._main_subtitle_scale
        remembered_secondary_subtitle_scale = self._secondary_subtitle_scale
        remembered_main_subtitle_scale_supported = self._main_subtitle_scale_supported
        remembered_secondary_subtitle_scale_supported = self._secondary_subtitle_scale_supported
        try:
            self._subtitle_tracks = self.video.subtitle_tracks()
        except Exception as exc:
            self._subtitle_tracks = []
            self._subtitle_preference = SubtitlePreference()
            self._reset_subtitle_combo()
            self._append_log(f"字幕加载失败: {exc}")
            return
        if self._subtitle_tracks and self._should_recheck_subtitle_tracks_after_stale_snapshot():
            try:
                self._subtitle_tracks = self.video.subtitle_tracks()
            except Exception:
                pass
        self._populate_subtitle_combo(self._subtitle_tracks)
        if manual_switch_refresh:
            if not self._subtitle_tracks:
                self._sync_subtitle_combo_without_tracks()
                return
            self._sync_subtitle_combo_to_preference()
            return
        if hasattr(self.video, "subtitle_position"):
            self._main_subtitle_position = self.video.subtitle_position()
        self._secondary_subtitle_position_supported = bool(
            getattr(self.video, "supports_secondary_subtitle_position", lambda: hasattr(self.video, "secondary_subtitle_position"))()
        )
        if self._secondary_subtitle_position_supported and hasattr(self.video, "secondary_subtitle_position"):
            self._secondary_subtitle_position = self.video.secondary_subtitle_position()
        self._main_subtitle_scale_supported = bool(
            getattr(self.video, "supports_subtitle_scale", lambda: hasattr(self.video, "subtitle_scale"))()
        )
        self._secondary_subtitle_scale_supported = bool(
            getattr(
                self.video,
                "supports_secondary_subtitle_scale",
                lambda: hasattr(self.video, "secondary_subtitle_scale"),
            )()
        )
        if self._main_subtitle_scale_supported and hasattr(self.video, "subtitle_scale"):
            current_main_subtitle_scale = self.video.subtitle_scale()
            if remembered_main_subtitle_scale_supported:
                self._main_subtitle_scale = remembered_main_subtitle_scale
            else:
                self._main_subtitle_scale = current_main_subtitle_scale
        if self._secondary_subtitle_scale_supported and hasattr(self.video, "secondary_subtitle_scale"):
            current_secondary_subtitle_scale = self.video.secondary_subtitle_scale()
            if remembered_secondary_subtitle_scale_supported:
                self._secondary_subtitle_scale = remembered_secondary_subtitle_scale
            else:
                self._secondary_subtitle_scale = current_secondary_subtitle_scale
        if not self._subtitle_tracks:
            try:
                if self._reload_selected_primary_external_subtitle_if_needed():
                    return
                if self._primary_external_subtitle_track_needs_reapply():
                    if not self._apply_primary_external_subtitle_track(self._primary_external_subtitle_track_id):
                        return
                    self._sync_subtitle_combo_without_tracks()
                    return
                if self._auto_apply_spider_subtitle_if_needed():
                    return
            except Exception as exc:
                self._append_log(f"字幕切换失败: {exc}")
                self._clear_primary_external_subtitle()
            self._sync_subtitle_combo_without_tracks()
            return
        skip_primary_subtitle_preference = bool(
            self._danmaku_loading_slot == "primary" or (self._danmaku_active and self._danmaku_uses_secondary_slot is False)
        )
        skip_secondary_subtitle_preference = bool(
            self._danmaku_loading_slot == "secondary" or (self._danmaku_active and self._danmaku_uses_secondary_slot is True)
        )
        if not skip_primary_subtitle_preference:
            try:
                self._apply_subtitle_preference()
            except Exception as exc:
                self._subtitle_preference = SubtitlePreference()
                self._reset_subtitle_combo()
                self._append_log(f"字幕切换失败: {exc}")
        if not self._danmaku_active and not skip_secondary_subtitle_preference and hasattr(self.video, "apply_secondary_subtitle_mode"):
            try:
                self._apply_secondary_subtitle_preference()
            except Exception as exc:
                self._secondary_subtitle_preference = SecondarySubtitlePreference()
                self._append_log(f"次字幕切换失败: {exc}")
        if hasattr(self.video, "set_subtitle_position"):
            try:
                self.video.set_subtitle_position(self._main_subtitle_position)
            except Exception as exc:
                self._append_log(f"主字幕位置设置失败: {exc}")
        if (
            not self._danmaku_active
            and self._secondary_subtitle_position_supported
            and hasattr(self.video, "set_secondary_subtitle_position")
        ):
            try:
                self.video.set_secondary_subtitle_position(self._secondary_subtitle_position)
            except Exception as exc:
                self._append_log(f"次字幕位置设置失败: {exc}")
        if (
            self._main_subtitle_scale_supported
            and hasattr(self.video, "set_subtitle_scale")
            and not (self._danmaku_active and self._danmaku_uses_secondary_slot is False)
        ):
            try:
                self.video.set_subtitle_scale(self._main_subtitle_scale)
            except Exception as exc:
                self._append_log(f"主字幕大小设置失败: {exc}")
        if (
            not self._danmaku_active
            and self._secondary_subtitle_scale_supported
            and hasattr(self.video, "set_secondary_subtitle_scale")
        ):
            try:
                self.video.set_secondary_subtitle_scale(self._secondary_subtitle_scale)
            except Exception as exc:
                self._append_log(f"次字幕大小设置失败: {exc}")

    def _refresh_audio_state(self) -> None:
        if self._skip_audio_refresh_for_manual_subtitle_switch and self._manual_subtitle_switch_refresh_active():
            self._clear_manual_subtitle_switch_refresh()
            return
        if not hasattr(self.video, "audio_tracks") or not hasattr(self.video, "apply_audio_mode"):
            self._audio_tracks = []
            self._audio_preference = AudioPreference()
            self._reset_audio_combo()
            return
        try:
            self._audio_tracks = self.video.audio_tracks()
        except Exception as exc:
            self._audio_tracks = []
            self._audio_preference = AudioPreference()
            self._reset_audio_combo()
            self._append_log(f"音轨加载失败: {exc}")
            return
        self._populate_audio_combo(self._audio_tracks)
        if not self._audio_tracks:
            self._audio_preference = AudioPreference()
            return
        try:
            self._apply_audio_preference()
        except Exception as exc:
            self._audio_preference = AudioPreference()
            self._reset_audio_combo()
            self._append_log(f"音轨切换失败: {exc}")

    def _refresh_video_quality_state(self, prepared_url: str | None = None) -> None:
        current_item = self._current_play_item()
        if current_item is None:
            self._video_quality_options = []
            self._reset_video_quality_combo()
            return
        if current_item.playback_qualities:
            self._video_quality_options = list(current_item.playback_qualities)
            selected_quality_id = current_item.selected_playback_quality_id or current_item.playback_qualities[0].id
            current_item.selected_playback_quality_id = selected_quality_id
            self._populate_video_quality_combo(self._video_quality_options, selected_quality_id)
            return
        source_url = current_item.original_url or current_item.url
        if not source_url.startswith(self._DASH_DATA_URI_PREFIX):
            self._video_quality_options = []
            self._reset_video_quality_combo()
            return
        qualities_getter = getattr(self._m3u8_ad_filter, "dash_video_qualities", None)
        selected_getter = getattr(self._m3u8_ad_filter, "selected_dash_video_quality", None)
        if not callable(qualities_getter) or not callable(selected_getter):
            self._video_quality_options = []
            self._reset_video_quality_combo()
            return
        target_url = prepared_url or current_item.url
        self._video_quality_options = list(qualities_getter(target_url))
        selected_quality_id = selected_getter(target_url) or current_item.dash_video_id or None
        if selected_quality_id is not None:
            current_item.dash_video_id = selected_quality_id
        self._populate_video_quality_combo(self._video_quality_options, selected_quality_id)

    def _change_subtitle_selection(self, index: int) -> None:
        if index < 0:
            return
        item_data = self.subtitle_combo.itemData(index)
        if item_data is None:
            return
        if isinstance(item_data, tuple) and len(item_data) == 3:
            mode, track_id, external_subtitle = item_data
        else:
            mode, track_id = item_data
            external_subtitle = None
        self._suppress_auto_spider_subtitle_for_current_item()
        if mode == "auto":
            self._subtitle_preference = SubtitlePreference()
            self._mark_manual_subtitle_switch_refresh()
            self.video.apply_subtitle_mode("auto")
            self._clear_primary_external_subtitle()
            return
        if mode == "off":
            self._subtitle_preference = SubtitlePreference(mode="off")
            self._mark_manual_subtitle_switch_refresh()
            self.video.apply_subtitle_mode("off")
            self._clear_primary_external_subtitle()
            return
        if mode == "external" and external_subtitle is not None:
            previous_track_id = self._primary_external_subtitle_track_id
            try:
                loaded_track_id, subtitle_path = self._load_external_subtitle(external_subtitle, secondary=False)
            except Exception as exc:
                self._append_log(f"字幕切换失败: {exc}")
                return
            self._subtitle_preference = SubtitlePreference(mode="external")
            self._primary_external_subtitle_selection = ExternalSubtitleSelection(
                source=external_subtitle.source,
                option_url=external_subtitle.url,
                option_name=external_subtitle.name,
                option_lang=external_subtitle.lang,
                option_format=external_subtitle.format,
            )
            self._primary_external_subtitle_track_id = loaded_track_id
            self._primary_external_subtitle_path = subtitle_path
            if previous_track_id != loaded_track_id:
                self._remove_external_subtitle_track(previous_track_id)
            try:
                self._mark_manual_subtitle_switch_refresh()
                self._apply_primary_external_subtitle_track(loaded_track_id)
            except Exception as exc:
                self._append_log(f"字幕切换失败: {exc}")
            return
        track = next((track for track in self._subtitle_tracks if track.id == track_id), None)
        if track is None:
            return
        self._remember_track_preference(track)
        self._mark_manual_subtitle_switch_refresh()
        self.video.apply_subtitle_mode("track", track_id=track_id)
        self._clear_primary_external_subtitle()

    def _change_danmaku_selection(self, index: int) -> None:
        if index < 0 or not self._current_play_item_danmaku_xml():
            return
        self._save_preferred_danmaku_selection(index)
        if index == 1:
            self._clear_active_danmaku()
            return
        line_count = self._danmaku_line_count_from_combo_index(index)
        try:
            self._enable_danmaku(line_count)
        except Exception as exc:
            self._append_log(f"弹幕切换失败: {exc}")
            self._clear_active_danmaku()
            self._reset_danmaku_combo(enabled=True, current_index=1)

    def _change_video_quality_selection(self, index: int) -> None:
        if index < 0 or self.session is None:
            return
        current_item = self.session.playlist[self.current_index]
        target_quality_id = self.video_quality_combo.itemData(index)
        if not isinstance(target_quality_id, str) or not target_quality_id:
            return
        if current_item.playback_qualities:
            if target_quality_id == current_item.selected_playback_quality_id:
                return
            selected_quality = next(
                (quality for quality in current_item.playback_qualities if quality.id == target_quality_id and quality.url),
                None,
            )
            if selected_quality is None:
                return
            try:
                start_position_seconds = int(self.video.position_seconds() or 0)
            except Exception:
                start_position_seconds = 0
            previous_url = current_item.url
            previous_original_url = current_item.original_url
            previous_selected_quality_id = current_item.selected_playback_quality_id
            current_item.url = selected_quality.url
            current_item.original_url = selected_quality.url
            current_item.selected_playback_quality_id = target_quality_id
            if self._start_playback_prepare(
                previous_index=self.current_index,
                start_position_seconds=start_position_seconds,
                pause=not self.is_playing,
                previous_url=previous_url,
                previous_original_url=previous_original_url,
                previous_selected_playback_quality_id=previous_selected_quality_id,
            ):
                return
            try:
                self._start_current_item_playback(
                    start_position_seconds=start_position_seconds,
                    pause=not self.is_playing,
                )
            except Exception as exc:
                current_item.url = previous_url
                current_item.original_url = previous_original_url
                current_item.selected_playback_quality_id = previous_selected_quality_id
                self._refresh_video_quality_state()
                self._append_log(f"清晰度切换失败: {exc}")
            return
        if target_quality_id == current_item.dash_video_id:
            return
        source_url = current_item.original_url or current_item.url
        if not source_url.startswith(self._DASH_DATA_URI_PREFIX):
            return
        try:
            start_position_seconds = int(self.video.position_seconds() or 0)
        except Exception:
            start_position_seconds = 0
        self._start_playback_prepare(
            previous_index=self.current_index,
            start_position_seconds=start_position_seconds,
            pause=not self.is_playing,
            dash_video_id=target_quality_id,
        )

    def _change_audio_selection(self, index: int) -> None:
        if index < 0:
            return
        item_data = self.audio_combo.itemData(index)
        if item_data is None:
            return
        mode, track_id = item_data
        if mode == "auto":
            self._audio_preference = AudioPreference()
            self.video.apply_audio_mode("auto")
            return
        track = next((track for track in self._audio_tracks if track.id == track_id), None)
        if track is None:
            return
        self._remember_audio_track_preference(track)
        self.video.apply_audio_mode("track", track_id=track_id)

    def _show_video_context_menu(self, pos) -> None:
        global_pos = self.video_widget.mapToGlobal(pos)
        if self._should_ignore_video_context_menu_request(global_pos):
            return
        self._close_video_context_menu()
        menu = self._build_video_context_menu()
        self._video_context_menu = menu
        menu.aboutToHide.connect(lambda menu=menu: self._handle_video_context_menu_hidden(menu))
        menu.aboutToHide.connect(menu.deleteLater)
        menu.exec(global_pos)

    def _show_video_context_menu_from_widget(self, widget: QWidget, pos) -> None:
        mapped_pos = pos if widget is self.video_widget else self.video_widget.mapFromGlobal(widget.mapToGlobal(pos))
        self._show_video_context_menu(mapped_pos)

    def _show_video_context_menu_from_global_pos(self, global_pos) -> None:
        self._show_video_context_menu(self.video_widget.mapFromGlobal(global_pos))

    def _show_video_context_menu_at_cursor(self) -> None:
        self._show_video_context_menu_from_global_pos(QCursor.pos())

    def _dismiss_video_context_menu_at_cursor(self) -> None:
        global_pos = QCursor.pos()
        if not self._video_context_menu_contains_global_pos(global_pos):
            self._close_video_context_menu()

    def _contains_video_global_pos(self, global_pos) -> bool:
        return self.video_widget.isVisible() and self.video_widget.rect().contains(self.video_widget.mapFromGlobal(global_pos))

    def _video_context_menu_contains_global_pos(self, global_pos) -> bool:
        menu = self._video_context_menu
        menu_geometry = getattr(menu, "geometry", None)
        if (
            menu is None
            or not menu.isVisible()
            or menu_geometry is None
            or not menu_geometry().contains(global_pos)
        ):
            active_popup = QApplication.activePopupWidget()
            if not isinstance(active_popup, QMenu) or not active_popup.isVisible():
                return False
            if active_popup is not menu and active_popup.parentWidget() is not menu:
                return False
            return active_popup.geometry().contains(global_pos)
        return True

    def _should_ignore_video_context_menu_request(self, global_pos) -> bool:
        if self._video_context_menu_contains_global_pos(global_pos):
            return True
        last_pos = self._last_video_context_menu_request_global_pos
        now_ms = int(time.monotonic() * 1000)
        duplicate_window = now_ms - self._last_video_context_menu_request_ms <= self._VIDEO_CONTEXT_MENU_DUPLICATE_WINDOW_MS
        if last_pos is None or not duplicate_window:
            self._last_video_context_menu_request_ms = now_ms
            self._last_video_context_menu_request_global_pos = (global_pos.x(), global_pos.y())
            return False
        dx = abs(last_pos[0] - global_pos.x())
        dy = abs(last_pos[1] - global_pos.y())
        if dx <= self._VIDEO_CONTEXT_MENU_DUPLICATE_DISTANCE and dy <= self._VIDEO_CONTEXT_MENU_DUPLICATE_DISTANCE:
            return True
        self._last_video_context_menu_request_ms = now_ms
        self._last_video_context_menu_request_global_pos = (global_pos.x(), global_pos.y())
        return False

    def _handle_video_context_menu_hidden(self, menu: QMenu) -> None:
        if self._video_context_menu is menu:
            self._video_context_menu = None

    def _close_video_context_menu(self) -> bool:
        menu = self._video_context_menu
        if menu is None:
            return False
        if menu.isVisible():
            menu.hide()
            self._video_context_menu = None
            return True
        self._video_context_menu = None
        return False

    def _build_video_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addMenu(self._build_primary_subtitle_menu(menu))
        menu.addMenu(self._build_secondary_subtitle_menu(menu))
        menu.addMenu(self._build_subtitle_position_menu(menu, title="主字幕位置", secondary=False))
        menu.addMenu(self._build_subtitle_position_menu(menu, title="次字幕位置", secondary=True))
        menu.addMenu(self._build_subtitle_scale_menu(menu, title="主字幕大小", secondary=False))
        menu.addMenu(self._build_subtitle_scale_menu(menu, title="次字幕大小", secondary=True))
        menu.addMenu(self._build_audio_menu(menu))
        if self._video_quality_options:
            menu.addMenu(self._build_video_quality_menu(menu))
        menu.addMenu(self._build_danmaku_menu(menu))
        menu.addAction("弹幕源", self._open_danmaku_source_dialog)
        menu.addAction("弹幕设置", self._open_danmaku_settings_dialog)
        menu.addAction("视频信息", self._toggle_video_info_from_menu)
        return menu

    def _current_play_item(self) -> PlayItem | None:
        if self.session is None or not self.session.playlist:
            return None
        if not 0 <= self.current_index < len(self.session.playlist):
            return None
        return self.session.playlist[self.current_index]

    def _refresh_danmaku_source_entry_points(self) -> None:
        self.danmaku_source_button.setEnabled(True)
        self.danmaku_settings_button.setEnabled(True)

    def _refresh_danmaku_settings_color_controls(self) -> None:
        enabled = self._preferred_danmaku_color_mode() == "uniform"
        if self._danmaku_uniform_color_button is not None:
            self._danmaku_uniform_color_button.setEnabled(enabled)

    def _refresh_danmaku_settings_position_controls(self) -> None:
        enabled = self._preferred_danmaku_render_mode() != "static"
        if self._danmaku_position_preset_combo is not None:
            self._danmaku_position_preset_combo.setEnabled(enabled)

    def _refresh_danmaku_uniform_color_button(self) -> None:
        if self._danmaku_uniform_color_button is None:
            return
        color = self._preferred_danmaku_uniform_color()
        preview = QColor(color)
        foreground = "#000000" if preview.lightness() >= 160 else "#FFFFFF"
        self._danmaku_uniform_color_button.setText(color)
        self._danmaku_uniform_color_button.setStyleSheet(
            "text-align: left; padding: 6px 10px; border: 1px solid #888;"
            f" background-color: {color}; color: {foreground};"
        )

    def _ensure_danmaku_uniform_color_dialog(self) -> QColorDialog:
        if self._danmaku_uniform_color_dialog is not None:
            return self._danmaku_uniform_color_dialog
        dialog = QColorDialog(self)
        dialog.setWindowTitle("选择弹幕颜色")
        dialog.setOption(QColorDialog.ColorDialogOption.ShowAlphaChannel, False)
        dialog.currentColorChanged.connect(self._preview_danmaku_uniform_color)
        dialog.rejected.connect(self._restore_previewed_danmaku_uniform_color)
        dialog.accepted.connect(self._clear_danmaku_uniform_color_preview)
        self._danmaku_uniform_color_dialog = dialog
        return dialog

    def _open_danmaku_uniform_color_dialog(self) -> None:
        dialog = self._ensure_danmaku_uniform_color_dialog()
        self._danmaku_uniform_color_preview_original = self._preferred_danmaku_uniform_color()
        dialog.blockSignals(True)
        dialog.setCurrentColor(QColor(self._preferred_danmaku_uniform_color()))
        dialog.blockSignals(False)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _preview_danmaku_uniform_color(self, color: QColor) -> None:
        if not color.isValid():
            return
        self._save_danmaku_uniform_color(color.name().upper())

    def _restore_previewed_danmaku_uniform_color(self) -> None:
        if self._danmaku_uniform_color_preview_original is None:
            return
        self._save_danmaku_uniform_color(self._danmaku_uniform_color_preview_original)
        self._clear_danmaku_uniform_color_preview()

    def _clear_danmaku_uniform_color_preview(self) -> None:
        self._danmaku_uniform_color_preview_original = None

    def _refresh_danmaku_settings_dialog_controls(self) -> None:
        if self._danmaku_line_count_spin is not None:
            self._danmaku_line_count_spin.blockSignals(True)
            self._danmaku_line_count_spin.setValue(self._preferred_danmaku_line_count())
            self._danmaku_line_count_spin.blockSignals(False)
        if self._danmaku_render_mode_combo is not None:
            self._danmaku_render_mode_combo.blockSignals(True)
            self._danmaku_render_mode_combo.setCurrentIndex(
                max(0, self._danmaku_render_mode_combo.findData(self._preferred_danmaku_render_mode()))
            )
            self._danmaku_render_mode_combo.blockSignals(False)
        if self._danmaku_color_mode_combo is not None:
            self._danmaku_color_mode_combo.blockSignals(True)
            self._danmaku_color_mode_combo.setCurrentIndex(
                max(0, self._danmaku_color_mode_combo.findData(self._preferred_danmaku_color_mode()))
            )
            self._danmaku_color_mode_combo.blockSignals(False)
        self._refresh_danmaku_uniform_color_button()
        if self._danmaku_position_preset_combo is not None:
            self._danmaku_position_preset_combo.blockSignals(True)
            self._danmaku_position_preset_combo.setCurrentIndex(
                max(0, self._danmaku_position_preset_combo.findData(self._preferred_danmaku_position_preset()))
            )
            self._danmaku_position_preset_combo.blockSignals(False)
        if self._danmaku_font_size_spin is not None:
            self._danmaku_font_size_spin.blockSignals(True)
            self._danmaku_font_size_spin.setValue(self._preferred_danmaku_font_size())
            self._danmaku_font_size_spin.blockSignals(False)
        if self._danmaku_scroll_speed_spin is not None:
            self._danmaku_scroll_speed_spin.blockSignals(True)
            self._danmaku_scroll_speed_spin.setValue(self._preferred_danmaku_scroll_speed())
            self._danmaku_scroll_speed_spin.blockSignals(False)
        self._refresh_danmaku_settings_color_controls()
        self._refresh_danmaku_settings_position_controls()

    def _ensure_danmaku_settings_dialog(self) -> QDialog:
        if self._danmaku_settings_dialog is not None:
            return self._danmaku_settings_dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("弹幕设置")
        dialog.resize(420, 320)
        layout = QVBoxLayout(dialog)

        line_count_row = QHBoxLayout()
        line_count_row.addWidget(QLabel("显示行数", dialog))
        self._danmaku_line_count_spin = QSpinBox(dialog)
        self._danmaku_line_count_spin.setRange(1, 10)
        line_count_row.addWidget(self._danmaku_line_count_spin, 1)
        layout.addLayout(line_count_row)

        render_row = QHBoxLayout()
        render_row.addWidget(QLabel("显示模式", dialog))
        self._danmaku_render_mode_combo = QComboBox(dialog)
        self._danmaku_render_mode_combo.addItem("静态", "static")
        self._danmaku_render_mode_combo.addItem("仅滚动", "scroll_only")
        self._danmaku_render_mode_combo.addItem("混合", "mixed")
        render_row.addWidget(self._danmaku_render_mode_combo, 1)
        layout.addLayout(render_row)

        position_row = QHBoxLayout()
        position_row.addWidget(QLabel("位置预设", dialog))
        self._danmaku_position_preset_combo = QComboBox(dialog)
        self._danmaku_position_preset_combo.addItem("顶部", "top")
        self._danmaku_position_preset_combo.addItem("顶部偏下", "upper")
        self._danmaku_position_preset_combo.addItem("中上", "mid_upper")
        self._danmaku_position_preset_combo.addItem("底部", "bottom")
        position_row.addWidget(self._danmaku_position_preset_combo, 1)
        layout.addLayout(position_row)

        color_mode_row = QHBoxLayout()
        color_mode_row.addWidget(QLabel("颜色模式", dialog))
        self._danmaku_color_mode_combo = QComboBox(dialog)
        self._danmaku_color_mode_combo.addItem("统一颜色", "uniform")
        self._danmaku_color_mode_combo.addItem("保留原色", "source")
        color_mode_row.addWidget(self._danmaku_color_mode_combo, 1)
        layout.addLayout(color_mode_row)

        uniform_color_row = QHBoxLayout()
        uniform_color_row.addWidget(QLabel("统一颜色", dialog))
        self._danmaku_uniform_color_edit = None
        self._danmaku_uniform_color_button = QPushButton(dialog)
        uniform_color_row.addWidget(self._danmaku_uniform_color_button, 1)
        layout.addLayout(uniform_color_row)

        font_size_row = QHBoxLayout()
        font_size_row.addWidget(QLabel("文字大小", dialog))
        self._danmaku_font_size_spin = QSpinBox(dialog)
        self._danmaku_font_size_spin.setRange(16, 72)
        self._danmaku_font_size_spin.setSingleStep(2)
        font_size_row.addWidget(self._danmaku_font_size_spin, 1)
        layout.addLayout(font_size_row)

        scroll_speed_row = QHBoxLayout()
        scroll_speed_row.addWidget(QLabel("滚动速率", dialog))
        self._danmaku_scroll_speed_spin = QDoubleSpinBox(dialog)
        self._danmaku_scroll_speed_spin.setRange(0.5, 2.0)
        self._danmaku_scroll_speed_spin.setSingleStep(0.1)
        self._danmaku_scroll_speed_spin.setDecimals(1)
        self._danmaku_scroll_speed_spin.setSuffix("x")
        scroll_speed_row.addWidget(self._danmaku_scroll_speed_spin, 1)
        layout.addLayout(scroll_speed_row)

        actions = QHBoxLayout()
        actions.addStretch(1)
        reset_button = QPushButton("恢复默认", dialog)
        close_button = QPushButton("关闭", dialog)
        actions.addWidget(reset_button)
        actions.addWidget(close_button)
        layout.addLayout(actions)

        self._danmaku_line_count_spin.valueChanged.connect(self._save_danmaku_line_count)
        self._danmaku_render_mode_combo.currentIndexChanged.connect(
            lambda index: self._save_danmaku_render_mode(self._danmaku_render_mode_combo.itemData(index))
        )
        self._danmaku_color_mode_combo.currentIndexChanged.connect(
            lambda index: self._save_danmaku_color_mode(self._danmaku_color_mode_combo.itemData(index))
        )
        self._danmaku_uniform_color_button.clicked.connect(self._open_danmaku_uniform_color_dialog)
        self._danmaku_position_preset_combo.currentIndexChanged.connect(
            lambda index: self._save_danmaku_position_preset(self._danmaku_position_preset_combo.itemData(index))
        )
        self._danmaku_font_size_spin.valueChanged.connect(self._save_danmaku_font_size)
        self._danmaku_scroll_speed_spin.valueChanged.connect(self._save_danmaku_scroll_speed)
        reset_button.clicked.connect(self._restore_default_danmaku_render_settings)
        close_button.clicked.connect(dialog.close)

        self._danmaku_settings_dialog = dialog
        self._refresh_danmaku_settings_dialog_controls()
        return dialog

    def _restore_default_danmaku_render_settings(self) -> None:
        if self.config is None:
            return
        self.config.preferred_danmaku_render_mode = "static"
        self.config.preferred_danmaku_color_mode = "source"
        self.config.preferred_danmaku_uniform_color = "#FFFFFF"
        self.config.preferred_danmaku_position_preset = "top"
        self.config.preferred_danmaku_line_count = 1
        self.config.preferred_danmaku_scroll_speed = 1.0
        self.config.preferred_danmaku_font_size = 32
        self._save_config()
        self._refresh_danmaku_combo_from_preferences()
        self._refresh_danmaku_settings_dialog_controls()
        self._reload_active_danmaku_for_render_settings()

    def _open_danmaku_settings_dialog(self) -> None:
        dialog = self._ensure_danmaku_settings_dialog()
        self._refresh_danmaku_settings_dialog_controls()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _ensure_danmaku_source_dialog(self) -> QDialog:
        if self._danmaku_source_dialog is not None:
            return self._danmaku_source_dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("弹幕源")
        dialog.resize(760, 480)
        layout = QVBoxLayout(dialog)
        search_row = QHBoxLayout()
        title_column = QVBoxLayout()
        title_column.addWidget(QLabel("媒体标题", dialog))
        self._danmaku_source_title_edit = QLineEdit(dialog)
        title_column.addWidget(self._danmaku_source_title_edit)
        episode_column = QVBoxLayout()
        episode_column.addWidget(QLabel("集数", dialog))
        self._danmaku_source_episode_edit = QLineEdit(dialog)
        episode_column.addWidget(self._danmaku_source_episode_edit)
        source_column = QVBoxLayout()
        source_column.addWidget(QLabel("搜索来源", dialog))
        self._danmaku_source_search_provider_combo = QComboBox(dialog)
        for provider_key, provider_label in _DANMAKU_SEARCH_PROVIDER_OPTIONS:
            self._danmaku_source_search_provider_combo.addItem(provider_label, provider_key)
        source_column.addWidget(self._danmaku_source_search_provider_combo)
        search_row.addLayout(title_column, 2)
        search_row.addLayout(episode_column, 1)
        search_row.addLayout(source_column, 1)
        layout.addLayout(search_row)
        columns = QHBoxLayout()
        self._danmaku_source_provider_list = QListWidget(dialog)
        self._danmaku_source_option_list = QListWidget(dialog)
        columns.addWidget(self._danmaku_source_provider_list, 1)
        columns.addWidget(self._danmaku_source_option_list, 2)
        layout.addLayout(columns)
        self._danmaku_source_status_label = QLabel("", dialog)
        layout.addWidget(self._danmaku_source_status_label)
        actions = QHBoxLayout()
        rerun_button = QPushButton("重新搜索", dialog)
        self._danmaku_source_rerun_button = rerun_button
        reset_button = QPushButton("恢复默认搜索词", dialog)
        switch_button = QPushButton("切换并加载", dialog)
        rerun_button.clicked.connect(self._rerun_current_item_danmaku_search)
        reset_button.clicked.connect(self._reset_current_item_danmaku_search_query)
        switch_button.clicked.connect(self._switch_current_item_danmaku_source)
        actions.addWidget(rerun_button)
        actions.addWidget(reset_button)
        actions.addWidget(switch_button)
        layout.addLayout(actions)
        self._danmaku_source_provider_list.currentRowChanged.connect(self._handle_danmaku_source_provider_changed)
        self._danmaku_source_search_provider_combo.currentIndexChanged.connect(
            self._handle_danmaku_search_provider_changed
        )
        self._danmaku_source_dialog = dialog
        return dialog

    def _set_danmaku_search_provider_combo_value(self, provider_key: str) -> None:
        if self._danmaku_source_search_provider_combo is None:
            return
        target_index = 0
        for index in range(self._danmaku_source_search_provider_combo.count()):
            if self._danmaku_source_search_provider_combo.itemData(index) == provider_key:
                target_index = index
                break
        self._danmaku_source_search_provider_combo.blockSignals(True)
        self._danmaku_source_search_provider_combo.setCurrentIndex(target_index)
        self._danmaku_source_search_provider_combo.blockSignals(False)

    def _selected_danmaku_search_provider_from_dialog(self) -> str:
        if self._danmaku_source_search_provider_combo is None:
            return ""
        return str(self._danmaku_source_search_provider_combo.currentData() or "")

    def _danmaku_provider_label(self, provider_key: str) -> str:
        for key, label in _DANMAKU_SEARCH_PROVIDER_OPTIONS:
            if key == provider_key:
                return label
        return provider_key or "全部"

    def _selected_danmaku_source_provider_from_dialog(self) -> str:
        current_item = self._current_play_item()
        selected_url = self._selected_danmaku_source_url_from_dialog()
        if current_item is None or not selected_url:
            return current_item.selected_danmaku_provider if current_item is not None else ""
        for group in current_item.danmaku_candidates:
            for option in group.options:
                if option.url == selected_url:
                    return option.provider
        return current_item.selected_danmaku_provider

    def _populate_danmaku_source_provider_list(self, groups) -> None:
        if self._danmaku_source_provider_list is None:
            return
        self._danmaku_source_provider_list.clear()
        for group in groups:
            self._danmaku_source_provider_list.addItem(f"{group.provider_label} ({len(group.options)})")
        if groups:
            self._danmaku_source_provider_list.setCurrentRow(0)

    def _format_danmaku_source_duration(self, duration_seconds: int) -> str:
        if duration_seconds <= 0:
            return ""
        hours, remainder = divmod(int(duration_seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def _populate_danmaku_source_option_list(self, groups, selected_provider: str) -> None:
        if self._danmaku_source_option_list is None:
            return
        self._danmaku_source_option_list.clear()
        target_group = None
        for group in groups:
            if group.provider == selected_provider:
                target_group = group
                break
        if target_group is None and groups:
            target_group = groups[0]
        if target_group is None:
            return
        current_item = self._current_play_item()
        selected_url = current_item.selected_danmaku_url if current_item is not None else ""
        selected_index = 0
        for index, option in enumerate(target_group.options):
            label = option.name
            duration_text = self._format_danmaku_source_duration(option.duration_seconds)
            if duration_text:
                label = f"{label} · {duration_text}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, option.url)
            self._danmaku_source_option_list.addItem(item)
            if option.url == selected_url:
                selected_index = index
        if self._danmaku_source_option_list.count():
            self._danmaku_source_option_list.setCurrentRow(selected_index)

    def _handle_danmaku_source_provider_changed(self, index: int) -> None:
        current_item = self._current_play_item()
        if current_item is None or index < 0 or index >= len(current_item.danmaku_candidates):
            return
        group = current_item.danmaku_candidates[index]
        self._populate_danmaku_source_option_list(current_item.danmaku_candidates, group.provider)

    def _handle_danmaku_search_provider_changed(self, _index: int) -> None:
        current_item = self._current_play_item()
        if current_item is None:
            return
        current_item.danmaku_search_provider = self._selected_danmaku_search_provider_from_dialog()

    def _open_danmaku_source_dialog(self) -> None:
        current_item = self._current_play_item()
        if current_item is None:
            return
        if (
            not current_item.danmaku_candidates
            and self.session is not None
            and self.session.danmaku_controller is not None
            and hasattr(self.session.danmaku_controller, "load_cached_danmaku_sources")
        ):
            self.session.danmaku_controller.load_cached_danmaku_sources(current_item)
        dialog = self._ensure_danmaku_source_dialog()
        if self._danmaku_source_title_edit is not None:
            self._danmaku_source_title_edit.setText(current_item.danmaku_search_title)
        if self._danmaku_source_episode_edit is not None:
            self._danmaku_source_episode_edit.setText(current_item.danmaku_search_episode)
        self._set_danmaku_search_provider_combo_value(current_item.danmaku_search_provider)
        self._populate_danmaku_source_provider_list(current_item.danmaku_candidates)
        self._populate_danmaku_source_option_list(current_item.danmaku_candidates, current_item.selected_danmaku_provider)
        self._refresh_danmaku_source_dialog_actions(current_item)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _refresh_danmaku_source_dialog_from_item(self, current_item: PlayItem) -> None:
        if self._danmaku_source_dialog is None:
            return
        if self._danmaku_source_title_edit is not None:
            self._danmaku_source_title_edit.setText(current_item.danmaku_search_title)
        if self._danmaku_source_episode_edit is not None:
            self._danmaku_source_episode_edit.setText(current_item.danmaku_search_episode)
        self._set_danmaku_search_provider_combo_value(current_item.danmaku_search_provider)
        self._populate_danmaku_source_provider_list(current_item.danmaku_candidates)
        self._populate_danmaku_source_option_list(current_item.danmaku_candidates, current_item.selected_danmaku_provider)
        self._refresh_danmaku_source_dialog_actions(current_item)
        self._refresh_danmaku_source_entry_points()

    def _refresh_danmaku_source_dialog_actions(self, current_item: PlayItem | None) -> None:
        if self._danmaku_source_rerun_button is not None:
            self._danmaku_source_rerun_button.setEnabled(bool(current_item is not None and not current_item.danmaku_pending))
        if self._danmaku_source_status_label is not None:
            self._danmaku_source_status_label.setText(current_item.danmaku_status_text if current_item is not None else "")

    def _start_danmaku_source_task(
        self,
        item: PlayItem,
        *,
        error_prefix: str,
        task: Callable[[], None],
        configure_danmaku_on_success: bool = False,
    ) -> None:
        if item.danmaku_pending:
            return
        item.danmaku_pending = True
        self._refresh_danmaku_source_dialog_from_item(item)

        def run() -> None:
            succeeded = False
            try:
                task()
                succeeded = True
            finally:
                item.danmaku_pending = False
                item.danmaku_status_text = ""
                self._danmaku_source_task_signals.finished.emit(item, configure_danmaku_on_success and succeeded)

        self._enqueue_controller_task(error_prefix, run)

    def _handle_danmaku_source_task_finished(self, item: PlayItem, configure_danmaku: bool) -> None:
        self._refresh_danmaku_source_dialog_from_item(item)
        current_item = self._current_play_item()
        if current_item is None or current_item is not item:
            return
        if configure_danmaku and item.danmaku_xml:
            self._configure_danmaku_for_current_item()

    def _selected_danmaku_source_url_from_dialog(self) -> str:
        if self._danmaku_source_option_list is None:
            return ""
        current_item = self._danmaku_source_option_list.currentItem()
        if current_item is None:
            return ""
        return str(current_item.data(Qt.ItemDataRole.UserRole) or "")

    def _current_media_duration_seconds(self) -> int:
        if not hasattr(self, "video") or not hasattr(self.video, "duration_seconds"):
            return 0
        try:
            return max(0, int(self.video.duration_seconds() or 0))
        except Exception:
            return 0

    def _rerun_current_item_danmaku_search(self) -> None:
        if (
            self.session is None
            or self.session.danmaku_controller is None
            or self._danmaku_source_title_edit is None
            or self._danmaku_source_episode_edit is None
        ):
            return
        current_item = self.session.playlist[self.current_index]
        title = self._danmaku_source_title_edit.text().strip()
        episode = self._danmaku_source_episode_edit.text().strip()
        current_item.danmaku_search_title = title
        current_item.danmaku_search_episode = episode
        current_item.danmaku_search_query = " ".join(part for part in (title, episode) if part).strip()
        current_item.danmaku_search_provider = self._selected_danmaku_search_provider_from_dialog()
        current_item.danmaku_status_text = (
            f"搜索中（{self._danmaku_provider_label(current_item.danmaku_search_provider)}）..."
        )
        current_item.danmaku_search_query_overridden = True
        media_duration_seconds = self._current_media_duration_seconds()
        self._start_danmaku_source_task(
            current_item,
            error_prefix="弹幕源重新搜索失败",
            task=lambda: self.session.danmaku_controller.refresh_danmaku_sources(
                current_item,
                search_title_override=title,
                search_episode_override=episode,
                force_refresh=True,
                media_duration_seconds=media_duration_seconds,
                provider_filter=current_item.danmaku_search_provider,
            ),
        )

    def _reset_current_item_danmaku_search_query(self) -> None:
        if self.session is None or self.session.danmaku_controller is None:
            return
        current_item = self.session.playlist[self.current_index]
        current_item.danmaku_search_title = ""
        current_item.danmaku_search_episode = ""
        current_item.danmaku_search_query = ""
        current_item.danmaku_search_provider = self._selected_danmaku_search_provider_from_dialog()
        current_item.danmaku_status_text = (
            f"搜索中（{self._danmaku_provider_label(current_item.danmaku_search_provider)}）..."
        )
        current_item.danmaku_search_query_overridden = False
        media_duration_seconds = self._current_media_duration_seconds()
        self._start_danmaku_source_task(
            current_item,
            error_prefix="弹幕源恢复默认搜索失败",
            task=lambda: self.session.danmaku_controller.refresh_danmaku_sources(
                current_item,
                query_override=None,
                force_refresh=True,
                media_duration_seconds=media_duration_seconds,
                provider_filter=current_item.danmaku_search_provider,
            ),
        )

    def _switch_current_item_danmaku_source(self) -> None:
        if self.session is None or self.session.danmaku_controller is None:
            return
        current_item = self.session.playlist[self.current_index]
        selected_url = self._selected_danmaku_source_url_from_dialog()
        if not selected_url:
            return
        selected_provider = self._selected_danmaku_source_provider_from_dialog()
        current_item.danmaku_status_text = f"下载中（{self._danmaku_provider_label(selected_provider)}）..."
        self._start_danmaku_source_task(
            current_item,
            error_prefix="弹幕切换失败",
            task=lambda: self.session.danmaku_controller.switch_danmaku_source(current_item, selected_url),
            configure_danmaku_on_success=True,
        )

    def _build_primary_subtitle_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu("主字幕", parent)
        group = QActionGroup(menu)
        group.setExclusive(True)
        primary_external_subtitle = self._current_primary_external_subtitle()

        auto_action = menu.addAction("自动选择")
        auto_action.setCheckable(True)
        auto_action.setChecked(primary_external_subtitle is None and self._subtitle_preference.mode == "auto")
        auto_action.triggered.connect(lambda: self._set_primary_subtitle_from_menu("auto", None))
        group.addAction(auto_action)

        off_action = menu.addAction("关闭字幕")
        off_action.setCheckable(True)
        off_action.setChecked(primary_external_subtitle is None and self._subtitle_preference.mode == "off")
        off_action.triggered.connect(lambda: self._set_primary_subtitle_from_menu("off", None))
        group.addAction(off_action)

        for track in self._subtitle_tracks:
            action = menu.addAction(track.label)
            action.setCheckable(True)
            action.setChecked(
                self._subtitle_preference.mode == "track"
                and self._subtitle_preference.title == track.title
                and self._subtitle_preference.lang == track.lang
            )
            action.triggered.connect(
                lambda _checked=False, track_id=track.id: self._set_primary_subtitle_from_menu("track", track_id)
            )
            group.addAction(action)

        for subtitle in self._current_item_external_subtitles():
            action = menu.addAction(subtitle.name)
            action.setCheckable(True)
            action.setChecked(
                self._primary_external_subtitle_selection is not None
                and self._primary_external_subtitle_selection.option_url == subtitle.url
            )
            action.triggered.connect(
                lambda _checked=False, subtitle_url=subtitle.url: self._set_primary_subtitle_from_menu(
                    "external",
                    subtitle_url,
                )
            )
            group.addAction(action)

        return menu

    def _build_secondary_subtitle_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu("次字幕", parent)
        group = QActionGroup(menu)
        group.setExclusive(True)

        off_action = menu.addAction("关闭次字幕")
        off_action.setCheckable(True)
        off_action.setChecked(
            self._secondary_external_subtitle_selection is None and self._secondary_subtitle_preference.mode == "off"
        )
        off_action.triggered.connect(lambda: self._set_secondary_subtitle_from_menu("off", None))
        group.addAction(off_action)

        for track in self._subtitle_tracks:
            action = menu.addAction(track.label)
            action.setCheckable(True)
            action.setChecked(
                self._secondary_subtitle_preference.mode == "track"
                and self._secondary_subtitle_preference.title == track.title
                and self._secondary_subtitle_preference.lang == track.lang
            )
            action.triggered.connect(
                lambda _checked=False, track_id=track.id: self._set_secondary_subtitle_from_menu("track", track_id)
            )
            group.addAction(action)

        for subtitle in self._current_item_secondary_external_subtitles():
            action = menu.addAction(subtitle.name)
            action.setCheckable(True)
            action.setChecked(
                self._secondary_external_subtitle_selection is not None
                and self._secondary_external_subtitle_selection.option_url == subtitle.url
            )
            action.triggered.connect(
                lambda _checked=False, subtitle_url=subtitle.url: self._set_secondary_subtitle_from_menu(
                    "external",
                    subtitle_url,
                )
            )
            group.addAction(action)

        return menu

    def _build_subtitle_position_menu(self, parent: QWidget, title: str, secondary: bool) -> QMenu:
        menu = QMenu(title, parent)
        if secondary and not self._secondary_subtitle_position_supported:
            menu.setEnabled(False)
            return menu
        group = QActionGroup(menu)
        group.setExclusive(True)
        current_value = self._secondary_subtitle_position if secondary else self._main_subtitle_position

        for label, value in self._SUBTITLE_POSITION_PRESETS.items():
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(current_value == value)
            action.triggered.connect(
                lambda _checked=False, value=value, secondary=secondary: self._set_subtitle_position_from_menu(
                    value,
                    secondary,
                )
            )
            group.addAction(action)

        menu.addSeparator()
        menu.addAction("上移 5%", lambda secondary=secondary: self._step_subtitle_position(-5, secondary))
        menu.addAction("下移 5%", lambda secondary=secondary: self._step_subtitle_position(5, secondary))
        menu.addAction("重置", lambda secondary=secondary: self._set_subtitle_position_from_menu(50, secondary))
        return menu

    def _build_audio_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu("音轨", parent)
        group = QActionGroup(menu)
        group.setExclusive(True)

        auto_action = menu.addAction("自动选择")
        auto_action.setCheckable(True)
        auto_action.setChecked(self._audio_preference.mode == "auto")
        auto_action.triggered.connect(lambda: self._set_audio_from_menu("auto", None))
        group.addAction(auto_action)

        for track in self._audio_tracks:
            action = menu.addAction(track.label)
            action.setCheckable(True)
            action.setChecked(
                self._audio_preference.mode == "track"
                and self._audio_preference.title == track.title
                and self._audio_preference.lang == track.lang
            )
            action.triggered.connect(lambda _checked=False, track_id=track.id: self._set_audio_from_menu("track", track_id))
            group.addAction(action)

        return menu

    def _build_video_quality_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu("清晰度", parent)
        if not self._video_quality_options:
            menu.setEnabled(False)
            return menu
        group = QActionGroup(menu)
        group.setExclusive(True)
        current_quality_id = self.video_quality_combo.currentData()
        for quality in self._video_quality_options:
            action = menu.addAction(quality.label)
            action.setCheckable(True)
            action.setChecked(current_quality_id == quality.id)
            action.triggered.connect(
                lambda _checked=False, quality_id=quality.id: self._set_video_quality_from_menu(quality_id)
            )
            group.addAction(action)
        menu.setEnabled(len(self._video_quality_options) > 1)
        return menu

    def _build_danmaku_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu("弹幕配置", parent)
        menu.setEnabled(self.danmaku_combo.isEnabled())
        group = QActionGroup(menu)
        group.setExclusive(True)

        for index in range(self.danmaku_combo.count()):
            label = "默认" if index == 0 else self.danmaku_combo.itemText(index)
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.danmaku_combo.currentIndex() == index)
            action.triggered.connect(lambda _checked=False, index=index: self._set_danmaku_from_menu(index))
            group.addAction(action)

        return menu

    def _build_subtitle_scale_menu(self, parent: QWidget, title: str, secondary: bool) -> QMenu:
        menu = QMenu(title, parent)
        if secondary and not self._secondary_subtitle_scale_supported:
            menu.setEnabled(False)
            return menu
        if not secondary and not self._main_subtitle_scale_supported:
            menu.setEnabled(False)
            return menu

        group = QActionGroup(menu)
        group.setExclusive(True)
        current_value = self._secondary_subtitle_scale if secondary else self._main_subtitle_scale

        for label, value in self._SUBTITLE_SCALE_PRESETS.items():
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(current_value == value)
            action.triggered.connect(
                lambda _checked=False, value=value, secondary=secondary: self._set_subtitle_scale_from_menu(value, secondary)
            )
            group.addAction(action)

        menu.addSeparator()
        menu.addAction("缩小 5%", lambda secondary=secondary: self._step_subtitle_scale(-5, secondary))
        menu.addAction("放大 5%", lambda secondary=secondary: self._step_subtitle_scale(5, secondary))
        menu.addAction("重置", lambda secondary=secondary: self._set_subtitle_scale_from_menu(100, secondary))
        return menu

    def _set_primary_subtitle_from_menu(self, mode: str, track_id: int | None) -> None:
        self._suppress_auto_spider_subtitle_for_current_item()
        if mode == "auto":
            self.subtitle_combo.setCurrentIndex(0)
            return
        if mode == "off":
            self.subtitle_combo.setCurrentIndex(1)
            return
        for index in range(self.subtitle_combo.count()):
            item_data = self.subtitle_combo.itemData(index)
            if item_data == ("track", track_id):
                self.subtitle_combo.setCurrentIndex(index)
                return
            if (
                isinstance(item_data, tuple)
                and len(item_data) == 3
                and item_data[0] == mode
                and ((mode == "track" and item_data[1] == track_id) or (mode == "external" and getattr(item_data[2], "url", None) == track_id))
            ):
                self.subtitle_combo.setCurrentIndex(index)
                return

    def _set_audio_from_menu(self, mode: str, track_id: int | None) -> None:
        if mode == "auto":
            self.audio_combo.setCurrentIndex(0)
            return
        for index in range(self.audio_combo.count()):
            if self.audio_combo.itemData(index) == ("track", track_id):
                self.audio_combo.setCurrentIndex(index)
                return

    def _set_video_quality_from_menu(self, quality_id: str) -> None:
        for index in range(self.video_quality_combo.count()):
            if self.video_quality_combo.itemData(index) == quality_id:
                self.video_quality_combo.setCurrentIndex(index)
                return

    def _set_danmaku_from_menu(self, index: int) -> None:
        if 0 <= index < self.danmaku_combo.count():
            self.danmaku_combo.setCurrentIndex(index)

    def _set_secondary_subtitle_from_menu(self, mode: str, track_id: int | None) -> None:
        try:
            if mode == "off":
                self.video.apply_secondary_subtitle_mode("off")
                self._secondary_subtitle_preference = SecondarySubtitlePreference()
                self._clear_secondary_external_subtitle()
                return
            if mode == "external":
                subtitle = self._find_current_item_external_subtitle(str(track_id or ""))
                if subtitle is None:
                    return
                previous_track_id = self._secondary_external_subtitle_track_id
                loaded_track_id, subtitle_path = self._load_external_subtitle(subtitle, secondary=True)
                self.video.apply_secondary_subtitle_mode("track", track_id=loaded_track_id)
                self._secondary_subtitle_preference = SecondarySubtitlePreference(mode="external")
                self._secondary_external_subtitle_selection = ExternalSubtitleSelection(
                    source=subtitle.source,
                    option_url=subtitle.url,
                    option_name=subtitle.name,
                    option_lang=subtitle.lang,
                    option_format=subtitle.format,
                )
                self._secondary_external_subtitle_track_id = loaded_track_id
                self._secondary_external_subtitle_path = subtitle_path
                if previous_track_id != loaded_track_id:
                    self._remove_external_subtitle_track(previous_track_id)
                return
            track = next((track for track in self._subtitle_tracks if track.id == track_id), None)
            if track is None:
                return
            self.video.apply_secondary_subtitle_mode("track", track_id=track.id)
            self._secondary_subtitle_preference = SecondarySubtitlePreference(
                mode="track",
                title=track.title,
                lang=track.lang,
                is_default=track.is_default,
                is_forced=track.is_forced,
            )
            self._clear_secondary_external_subtitle()
        except Exception as exc:
            self._append_log(f"次字幕切换失败: {exc}")

    def _write_external_subtitle_file(self, text: str, suffix: str) -> Path:
        temp_file = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=suffix, delete=False)
        try:
            temp_file.write(text)
        finally:
            temp_file.close()
        return Path(temp_file.name)

    def _fetch_external_subtitle_text(self, subtitle: ExternalSubtitleOption) -> str:
        subtitle_path = Path(subtitle.url)
        if subtitle_path.is_absolute() and subtitle_path.exists():
            return subtitle_path.read_text(encoding="utf-8")
        current_item = self._current_play_item()
        headers = {} if current_item is None else dict(current_item.headers)
        response = httpx.get(subtitle.url, headers=headers, timeout=10.0, follow_redirects=True)
        return str(getattr(response, "text", "") or "")

    def _load_external_subtitle(
        self,
        subtitle: ExternalSubtitleOption,
        *,
        secondary: bool,
    ) -> tuple[int | None, Path]:
        text = self._fetch_external_subtitle_text(subtitle)
        if not text.strip():
            raise ValueError("字幕内容为空")
        suffix = ".srt" if subtitle.format.endswith("subrip") else ".txt"
        subtitle_path = self._write_external_subtitle_file(text, suffix)
        track_id = self.video.load_external_subtitle(str(subtitle_path), select_for_secondary=secondary)
        return track_id, subtitle_path

    def _set_subtitle_position_from_menu(self, value: int, secondary: bool) -> None:
        clamped = max(0, min(int(value), 100))
        if secondary and not self._secondary_subtitle_position_supported:
            return
        try:
            if secondary:
                self.video.set_secondary_subtitle_position(clamped)
                self._secondary_subtitle_position = clamped
            else:
                self.video.set_subtitle_position(clamped)
                self._main_subtitle_position = clamped
        except Exception as exc:
            label = "次字幕位置设置失败" if secondary else "主字幕位置设置失败"
            self._append_log(f"{label}: {exc}")

    def _step_subtitle_position(self, delta: int, secondary: bool) -> None:
        current = self._secondary_subtitle_position if secondary else self._main_subtitle_position
        self._set_subtitle_position_from_menu(current + delta, secondary)

    def _set_subtitle_scale_from_menu(self, value: int, secondary: bool) -> None:
        clamped = max(50, min(int(value), 200))
        try:
            if secondary:
                if not self._secondary_subtitle_scale_supported:
                    return
                self.video.set_secondary_subtitle_scale(clamped)
                self._secondary_subtitle_scale = clamped
            else:
                if not self._main_subtitle_scale_supported:
                    return
                self.video.set_subtitle_scale(clamped)
                self._main_subtitle_scale = clamped
        except Exception as exc:
            label = "次字幕大小设置失败" if secondary else "主字幕大小设置失败"
            self._append_log(f"{label}: {exc}")

    def _step_subtitle_scale(self, delta: int, secondary: bool) -> None:
        current = self._secondary_subtitle_scale if secondary else self._main_subtitle_scale
        self._set_subtitle_scale_from_menu(current + delta, secondary)

    def _toggle_video_info_from_menu(self) -> None:
        try:
            self.video.toggle_video_info()
        except Exception as exc:
            self._append_log(f"视频信息显示失败: {exc}")

    def _change_volume(self, value: int) -> None:
        try:
            self.video.set_volume(value)
        except Exception as exc:
            self._append_log(f"音量设置失败: {exc}")
            return
        if self.config is not None and self.config.player_volume != value:
            self.config.player_volume = value
            self._save_config()

    def _step_volume(self, delta: int) -> None:
        value = max(self.volume_slider.minimum(), min(self.volume_slider.value() + delta, self.volume_slider.maximum()))
        self.volume_slider.setValue(value)

    def _speed_text(self, speed: float) -> str:
        text = f"{speed:.2f}".rstrip("0").rstrip(".")
        if "." not in text:
            text += ".0"
        return text + "x"

    def _current_speed_index(self) -> int:
        speeds = [float(self.speed_combo.itemText(index).rstrip("x")) for index in range(self.speed_combo.count())]
        return min(
            range(len(speeds)),
            key=lambda index: abs(speeds[index] - self.current_speed),
        )

    def _step_speed(self, delta: int) -> None:
        if self.speed_combo.count() == 0:
            return
        current_index = self._current_speed_index()
        new_index = max(0, min(current_index + delta, self.speed_combo.count() - 1))
        if new_index == self.speed_combo.currentIndex():
            self._change_speed(self.speed_combo.itemText(new_index))
            return
        self.speed_combo.setCurrentIndex(new_index)

    def _reset_speed(self) -> None:
        speed_index = self.speed_combo.findText("1.0x")
        if speed_index < 0:
            return
        if speed_index == self.speed_combo.currentIndex():
            self._change_speed("1.0x")
            return
        self.speed_combo.setCurrentIndex(speed_index)

    def _register_shortcuts(self) -> None:
        bindings = [
            (QKeySequence(Qt.Key.Key_Space), self.toggle_playback),
            (QKeySequence(Qt.Key.Key_Return), self.toggle_fullscreen),
            (QKeySequence(Qt.Key.Key_Enter), self.toggle_fullscreen),
            (QKeySequence("W"), self.wide_button.click),
            (QKeySequence("D"), self._open_danmaku_source_dialog),
            (QKeySequence("Ctrl+D"), self._open_danmaku_settings_dialog),
            (QKeySequence("I"), self._toggle_video_info_from_menu),
            (QKeySequence("M"), self._toggle_mute),
            (QKeySequence("-"), lambda: self._step_speed(-1)),
            (QKeySequence("+"), lambda: self._step_speed(1)),
            (QKeySequence("="), self._reset_speed),
            (QKeySequence(Qt.Key.Key_Down), lambda: self._step_volume(-self._VOLUME_SHORTCUT_STEP)),
            (QKeySequence(Qt.Key.Key_Up), lambda: self._step_volume(self._VOLUME_SHORTCUT_STEP)),
            (QKeySequence(Qt.Key.Key_Left), lambda: self._seek_relative(-self._SEEK_SHORTCUT_SECONDS)),
            (QKeySequence(Qt.Key.Key_Right), lambda: self._seek_relative(self._SEEK_SHORTCUT_SECONDS)),
            (
                QKeySequence("Ctrl+Left"),
                lambda: self._seek_relative(-self._MODIFIED_SEEK_SHORTCUT_SECONDS),
            ),
            (
                QKeySequence("Ctrl+Right"),
                lambda: self._seek_relative(self._MODIFIED_SEEK_SHORTCUT_SECONDS),
            ),
            (QKeySequence(Qt.Key.Key_PageUp), self.play_previous),
            (QKeySequence(Qt.Key.Key_PageDown), self.play_next),
        ]
        for sequence, handler in bindings:
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(handler)
            self._shortcut_bindings.append(shortcut)

    def _handle_slider_pressed(self) -> None:
        self._slider_dragging = True

    def _seek_from_slider(self) -> None:
        self._slider_dragging = False
        self._seek_to_position(self.progress.value())

    def _seek_to_position(self, seconds: int) -> None:
        try:
            self.video.seek(seconds)
        except Exception as exc:
            self._append_log(f"跳转失败: {exc}")

    def _sync_progress_slider(self) -> None:
        if self._slider_dragging:
            return
        duration = self.video.duration_seconds() if hasattr(self.video, "duration_seconds") else 0
        position = self.video.position_seconds() or 0
        if (
            not self._auto_advance_locked
            and self.session is not None
            and self.current_index + 1 < len(self.session.playlist)
            and duration > self.opening_spin.value() + self.ending_spin.value()
            and position < duration
            and position + self.ending_spin.value() >= duration
        ):
            self._auto_advance_locked = True
            self.play_next()
            return
        self.progress.setMaximum(max(duration, 0))
        self.progress.setValue(max(min(position, self.progress.maximum()), 0))
        self.current_time_label.setText(self._format_time(position))
        self.duration_label.setText(self._format_time(duration))

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            if self._was_maximized_before_fullscreen:
                self.showMaximized()
            else:
                self.showNormal()
            self._apply_visibility_state()
            return
        self._remember_sidebar_sizes()
        self._was_maximized_before_fullscreen = self.isMaximized()
        self.showFullScreen()
        self._apply_visibility_state()

    def _apply_visibility_state(self) -> None:
        is_fullscreen = self.isFullScreen()
        sidebar_hidden = is_fullscreen or self.wide_button.isChecked()
        self.bottom_area.setHidden(is_fullscreen)
        self.sidebar_actions_widget.setHidden(is_fullscreen)
        self.sidebar_container.setHidden(sidebar_hidden)
        hide_playlist = is_fullscreen or not self.toggle_playlist_button.isChecked()
        hide_details = is_fullscreen or not self.toggle_details_button.isChecked()
        hide_log = is_fullscreen or not self.toggle_log_button.isChecked()
        self.playlist_section.setHidden(hide_playlist)
        self.playlist.setHidden(hide_playlist)
        self.details.setHidden(hide_details)
        self.log_panel.setHidden(hide_log)
        if hasattr(self, "sidebar_splitter"):
            self._update_playlist_height()

    def _format_time(self, seconds: int) -> str:
        total_seconds = max(int(seconds), 0)
        minutes, remaining_seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"
        return f"{minutes:02d}:{remaining_seconds:02d}"

    def _restore_main_splitter_state(self) -> None:
        if self.config is None or not self.config.player_main_splitter_state:
            self.main_splitter.setSizes(self._DEFAULT_MAIN_SPLITTER_SIZES)
            return
        restored = self.main_splitter.restoreState(to_qbytearray(self.config.player_main_splitter_state))
        if not restored or self._has_collapsed_main_splitter_sizes():
            self.main_splitter.setSizes(self._DEFAULT_MAIN_SPLITTER_SIZES)

    def _has_collapsed_main_splitter_sizes(self) -> bool:
        sizes = self.main_splitter.sizes()
        return len(sizes) != 2 or any(size <= 0 for size in sizes)

    def _has_collapsed_splitter_sizes(self, sizes: list[int]) -> bool:
        return len(sizes) != 2 or any(size <= 0 for size in sizes)

    def _remember_sidebar_sizes(self) -> None:
        sizes = self.main_splitter.sizes()
        if self._has_collapsed_splitter_sizes(sizes):
            return
        self._sidebar_sizes = sizes

    def _restoreable_sidebar_sizes(self) -> list[int]:
        sizes = getattr(self, "_sidebar_sizes", self._DEFAULT_MAIN_SPLITTER_SIZES)
        if self._has_collapsed_splitter_sizes(sizes):
            return self._DEFAULT_MAIN_SPLITTER_SIZES
        return sizes

    def _main_splitter_state_for_persistence(self) -> bytes:
        if not self.wide_button.isChecked():
            return qbytearray_to_bytes(self.main_splitter.saveState())
        current_sizes = self.main_splitter.sizes()
        try:
            self.main_splitter.setSizes(self._restoreable_sidebar_sizes())
            return qbytearray_to_bytes(self.main_splitter.saveState())
        finally:
            self.main_splitter.setSizes(current_sizes)

    def _persist_geometry(self) -> None:
        if self.config is None:
            return
        self.config.player_window_geometry = qbytearray_to_bytes(self.saveGeometry())
        self.config.player_main_splitter_state = self._main_splitter_state_for_persistence()
        self._save_config()

    def _quit_application(self) -> None:
        self._quit_requested = True
        self._invalidate_play_item_resolution()
        self.report_progress(force_remote_report=True)
        self._stop_current_playback()
        if self.config is not None:
            self.config.last_active_window = "player"
        self._set_last_player_paused(not self.is_playing)
        self._restore_video_cursor()
        self._persist_geometry()
        app = QApplication.instance()
        if app is not None:
            app.quit()

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

    def _close_help_dialog(self) -> None:
        dialog = self.help_dialog
        if dialog is None:
            return
        self.help_dialog = None
        dialog.close()

    def _close_danmaku_source_dialog(self) -> None:
        dialog = self._danmaku_source_dialog
        if dialog is None or not dialog.isVisible():
            return
        dialog.close()

    def _close_danmaku_settings_dialog(self) -> None:
        dialog = self._danmaku_settings_dialog
        if dialog is None or not dialog.isVisible():
            return
        dialog.close()

    def _dismiss_escape_dialog(self) -> bool:
        dialog = self._danmaku_settings_dialog
        if dialog is not None and dialog.isVisible():
            self._close_danmaku_settings_dialog()
            return True
        dialog = self._danmaku_source_dialog
        if dialog is not None and dialog.isVisible():
            self._close_danmaku_source_dialog()
            return True
        if self.help_dialog is not None and self.help_dialog.isVisible():
            self._close_help_dialog()
            return True
        return False

    def _return_to_main(self) -> None:
        self._close_help_dialog()
        self._close_danmaku_source_dialog()
        self._close_danmaku_settings_dialog()
        self._close_video_context_menu()
        self._remember_restore_state()
        try:
            self.video.pause()
        except Exception:
            pass
        self.is_playing = False
        self.report_progress(force_remote_report=True)
        self._invalidate_play_item_resolution()
        self._stop_current_playback()
        self._refresh_window_title()
        self._restore_video_cursor()
        self._set_last_player_paused(True)
        self._update_play_button_icon()
        if self.config is not None:
            self.config.last_active_window = "main"
        self._persist_geometry()
        self.video_widget.shutdown()
        self.hide()
        self.closed_to_main.emit()

    def resume_from_main(self) -> None:
        if self.session is None:
            return
        self.is_playing = True
        self._set_last_player_paused(False)
        try:
            self._play_item_at_index(
                self.session.start_index,
                start_position_seconds=self.session.start_position_seconds,
            )
        except Exception as exc:
            self.is_playing = False
            self._set_last_player_paused(True)
            self._append_log(f"恢复播放失败: {exc}")
        self._update_play_button_icon()
        self._refresh_window_title()
        self._sync_video_cursor_autohide()

    def _handle_escape(self) -> None:
        if self._dismiss_escape_dialog():
            return
        if self.isFullScreen():
            self.toggle_fullscreen()
            return
        self._return_to_main()

    def toggle_playback(self) -> None:
        if self.is_playing:
            self.video.pause()
        else:
            self.video.resume()
        self.is_playing = not self.is_playing
        self._set_last_player_paused(not self.is_playing)
        self._update_play_button_icon()
        self._refresh_window_title()
        self._sync_video_cursor_autohide()

    def play_previous(self) -> None:
        if self.session is None or self.current_index <= 0:
            return
        self.report_progress(force_remote_report=True)
        self._stop_current_playback()
        target_index = self.current_index - 1
        try:
            self._play_item_at_index(target_index, preserve_primary_external_subtitle_selection=True)
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")

    def play_next(self) -> None:
        if self.session is None or self.current_index + 1 >= len(self.session.playlist):
            return
        self.report_progress(force_remote_report=True)
        self._stop_current_playback()
        target_index = self.current_index + 1
        try:
            self._play_item_at_index(target_index, preserve_primary_external_subtitle_selection=True)
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")

    def _handle_playback_finished(self) -> None:
        if self.session is None:
            return
        if self.current_index + 1 >= len(self.session.playlist):
            self.report_progress(force_remote_report=True)
            self._stop_current_playback()
            return
        self.play_next()

    def _play_clicked_item(self, item: QListWidgetItem) -> None:
        row = self.playlist.row(item)
        if row == self.current_index or self.session is None:
            return
        self.report_progress(force_remote_report=True)
        self._stop_current_playback()
        try:
            self._play_item_at_index(row, preserve_primary_external_subtitle_selection=True)
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")

    def closeEvent(self, event: QCloseEvent) -> None:
        self._deactivate_async_guard()
        try:
            self._poster_request_id += 1
            self._video_poster_request_id += 1
            self._invalidate_play_item_resolution()
            self._video_surface_ready = False
            self._close_help_dialog()
            self._close_video_context_menu()
            self._clear_active_danmaku()
            self.report_progress(force_remote_report=True)
            self._stop_current_playback()
            self.session = None
        finally:
            self._shutdown_controller_task_queue()
            self.report_timer.stop()
            self.progress_timer.stop()
            self._restore_video_cursor()
            self.video_widget.shutdown()
            app = QApplication.instance()
            if self._app_event_filter_installed and app is not None:
                app.removeEventFilter(self)
                self._app_event_filter_installed = False
        self._persist_geometry()
        if not self._quit_requested and self.config is not None:
            self.config.last_active_window = "main"
            self._save_config()
            self.closed_to_main.emit()
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:
        if all(hasattr(self, name) for name in ("playlist", "details", "playlist_section", "log_panel")):
            self._update_metadata_height()
            self._update_playlist_height()
        super().resizeEvent(event)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
            global_pos = event.globalPosition().toPoint()
            if (
                event.button() == Qt.MouseButton.LeftButton
                and self._video_context_menu is not None
                and not self._video_context_menu_contains_global_pos(global_pos)
            ):
                self._close_video_context_menu()
        if event.type() == QEvent.Type.ContextMenu and isinstance(event, QContextMenuEvent):
            if self._video_context_menu_contains_global_pos(event.globalPos()):
                return False
        if event.type() == QEvent.Type.MouseMove and self._belongs_to_player_window(watched):
            self._refresh_video_pointer_inside_state()
            if self.is_playing and self._video_pointer_inside:
                self._handle_video_mouse_activity()
            elif self.is_playing:
                self._restore_video_cursor(stop_timer=False, disable_native_autohide=False)
                if not self._cursor_hide_timer.isActive():
                    self._cursor_hide_timer.start()
            else:
                self._restore_video_cursor()
        if isinstance(watched, QWidget) and watched in self._video_surface_widgets():
            if event.type() == QEvent.Type.Enter:
                self._video_pointer_inside = True
                self._handle_video_mouse_activity()
            elif event.type() == QEvent.Type.MouseMove:
                self._video_pointer_inside = True
                self._handle_video_mouse_activity()
            elif event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
                if self._video_context_menu_contains_global_pos(event.globalPosition().toPoint()):
                    return False
                if event.button() == Qt.MouseButton.LeftButton and self._close_video_context_menu():
                    event.accept()
                    return True
                if event.button() == Qt.MouseButton.RightButton:
                    self._show_video_context_menu_from_widget(watched, event.position().toPoint())
                    event.accept()
                    return True
            elif event.type() == QEvent.Type.ContextMenu and isinstance(event, QContextMenuEvent):
                self._show_video_context_menu_from_global_pos(event.globalPos())
                event.accept()
                return True
            elif event.type() == QEvent.Type.Leave:
                self._handle_video_leave()
        elif (
            isinstance(watched, QWindow)
            and (
                (
                    event.type() == QEvent.Type.MouseButtonPress
                    and isinstance(event, QMouseEvent)
                    and event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton)
                    and self._contains_video_global_pos(event.globalPosition().toPoint())
                )
                or (
                    event.type() == QEvent.Type.ContextMenu
                    and isinstance(event, QContextMenuEvent)
                    and self._contains_video_global_pos(event.globalPos())
                )
            )
        ):
            global_pos = event.globalPosition().toPoint() if isinstance(event, QMouseEvent) else event.globalPos()
            if self._video_context_menu_contains_global_pos(global_pos):
                return False
            if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton and self._close_video_context_menu():
                event.accept()
                return True
            if isinstance(event, QMouseEvent):
                if event.button() == Qt.MouseButton.RightButton:
                    self._show_video_context_menu_from_global_pos(global_pos)
                    event.accept()
                    return True
                return False
            self._show_video_context_menu_from_global_pos(global_pos)
            event.accept()
            return True
        if not isinstance(watched, QObject):
            return False
        return super().eventFilter(cast(QObject, watched), event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_F1:
            self._show_shortcut_help()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self._handle_escape()
            event.accept()
            return
        if event.key() == Qt.Key.Key_P and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._return_to_main()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Left and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._seek_relative(-self._MODIFIED_SEEK_SHORTCUT_SECONDS)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._seek_relative(self._MODIFIED_SEEK_SHORTCUT_SECONDS)
            event.accept()
            return
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        ):
            super().keyPressEvent(event)
            return
        if event.key() == Qt.Key.Key_Space:
            self.toggle_playback()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggle_fullscreen()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Down:
            self._step_volume(-self._VOLUME_SHORTCUT_STEP)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Up:
            self._step_volume(self._VOLUME_SHORTCUT_STEP)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Left:
            self._seek_relative(-self._SEEK_SHORTCUT_SECONDS)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right:
            self._seek_relative(self._SEEK_SHORTCUT_SECONDS)
            event.accept()
            return
        if event.key() == Qt.Key.Key_PageUp:
            self.play_previous()
            event.accept()
            return
        if event.key() == Qt.Key.Key_PageDown:
            self.play_next()
            event.accept()
            return
        key_text = event.text().lower()
        if key_text == "m":
            self._toggle_mute()
            event.accept()
            return
        if key_text == "w":
            self.wide_button.click()
            event.accept()
            return
        if key_text == "d":
            self._open_danmaku_source_dialog()
            event.accept()
            return
        if key_text == "i":
            self._toggle_video_info_from_menu()
            event.accept()
            return
        if key_text == "-":
            self._step_speed(-1)
            event.accept()
            return
        if key_text == "+":
            self._step_speed(1)
            event.accept()
            return
        if key_text == "=":
            self._reset_speed()
            event.accept()
            return
        super().keyPressEvent(event)
