from __future__ import annotations

from copy import deepcopy
import html
import inspect
import json
import logging
import queue
import re
import struct
import sys
import threading
import time
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import httpx
from PySide6.QtCore import QEvent, QObject, QRect, QSize, QTimer, Qt, QUrl, QUrlQuery, Signal
from PySide6.QtGui import (
    QActionGroup,
    QBrush,
    QCloseEvent,
    QColor,
    QContextMenuEvent,
    QCursor,
    QDesktopServices,
    QIcon,
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPixmap,
    QShortcut,
    QWindow,
)
from PySide6.QtWidgets import QApplication, QInputDialog, QMenu, QStyle, QStyleOptionSlider, QToolTip
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QColorDialog,
    QDoubleSpinBox,
    QDialog,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedLayout,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from atv_player.danmaku.cache import load_or_create_danmaku_ass_cache
from atv_player.danmaku.generic import normalize_danmaku_episode_url
from atv_player.danmaku.utils import extract_official_link_url, infer_playlist_episode_number
from atv_player.subtitles.cache import save_subtitle_file
from atv_player.subtitles.service import build_subtitle_query
from atv_player.heat import has_required_heat_external_id, heat_identity_from_vod
from atv_player.metadata.bindings import (
    bilibili_season_binding_title,
    normalize_metadata_binding_title,
)
from atv_player.metadata.cache import MetadataCache
from atv_player.metadata.dialog_cache import (
    MetadataScrapeDialogState,
    load_cached_metadata_scrape_dialog_state,
    save_cached_metadata_scrape_dialog_state,
)
from atv_player.metadata.matching import normalize_match_title, strip_match_season_suffix
from atv_player.metadata.models import MetadataContext, MetadataQuery
from atv_player.metadata.query import normalize_metadata_query_inputs
from atv_player.metadata.scrape import normalize_metadata_scrape_title
from atv_player.metadata.episode_title_overrides import (
    apply_episode_title_overrides,
    episode_override_item_key,
)
from atv_player.metadata.providers.tmdb import infer_tmdb_media_type
from atv_player.controllers.browse_controller import clean_drive_directory_title, map_drive_video_to_play_item
from atv_player.controllers.player_controller import decode_drive_dir_id, split_drive_path
from atv_player.player.resume import drive_relative_path
from atv_player.playlist_sorting import format_size_bytes, parse_size_bytes
from atv_player.models import (
    ExternalSubtitleOption,
    ExternalSubtitleSelection,
    PlayItem,
    PlaybackSource,
    PlaybackSourceGroup,
    PlaybackDetailAction,
    PlaybackDetailField,
    PlaybackDetailFieldAction,
    PlaybackLoadResult,
    VideoQualityOption,
    VodItem,
    YtdlpAudioTrackOption,
)
from atv_player.episode_titles import normalize_episode_title_text, playlist_has_title_variants, playlist_item_display_title
from atv_player.log_store import AppLogEvent
from atv_player.player.bluray_iso import is_remote_iso_url
from atv_player.player.controls import PlayerControls
from atv_player.player.m3u8_ad_filter import M3U8AdFilter
from atv_player.player.mpv_widget import AudioTrack, Chapter, MpvWidget, SubtitleTrack, _render_profile_requires_shutdown
from atv_player.player.startup import PlaybackStartupCoordinator, PlaybackStartupStage, PlaybackStartupState
from atv_player.paths import app_cache_dir
from atv_player.playlist_sorting import (
    ORIGINAL,
    PlaylistSortState,
    find_playlist_item_index,
)
from atv_player.request_headers import normalize_media_request_headers
from atv_player.ui.async_guard import AsyncGuardMixin
from atv_player.ui.external_links import external_link_html
from atv_player.ui.help_dialog import ShortcutHelpDialog, show_shortcut_help_dialog
from atv_player.ui.icon_cache import load_icon, tint_icon
from atv_player.ui.poster_loader import load_remote_poster_image, normalize_poster_url, poster_cache_path
from atv_player.ui.qt_compat import qbytearray_to_bytes, to_qbytearray
from atv_player.ui.table_utils import configure_table_columns
from atv_player.ui.theme import (
    FlatComboBox,
    build_combobox_qss,
    build_combobox_popup_qss,
    build_compact_player_tabbar_qss,
    build_form_combobox_qss,
    build_form_line_edit_qss,
    build_form_spinbox_qss,
    build_player_control_button_qss,
    build_player_immersive_qss,
    build_player_list_qss,
    build_player_panel_qss,
    build_player_section_heading_qss,
    build_player_spinbox_qss,
    build_player_tabbar_qss,
    build_player_text_panel_qss,
    build_slider_qss,
    configure_form_flat_combobox,
    configure_flat_combobox,
    current_resolved_theme,
    current_theme_manager,
)
from atv_player.ui.toggle_switch import ToggleSwitch
from atv_player.ui.window_chrome import ThemedDialogBase, ThemedWidgetWindowBase
from atv_player.ui.x11_window import set_x11_window_above
from atv_player.yt_dlp_service import looks_like_youtube_video_id

_DANMAKU_SEARCH_PROVIDER_OPTIONS: list[tuple[str, str]] = [
    ("", "全部"),
    ("tencent", "腾讯"),
    ("youku", "优酷"),
    ("bilibili", "B站"),
    ("iqiyi", "爱奇艺"),
    ("mgtv", "芒果"),
    ("sohu", "搜狐"),
    ("migu", "咪咕"),
    ("renren", "人人"),
]

_METADATA_PROVIDER_LABELS: dict[str, str] = {
    "tencent": "腾讯",
    "iqiyi": "爱奇艺",
    "official_douban": "豆瓣官方",
    "local_douban": "本地豆瓣",
    "remote_douban": "本地豆瓣",
    "douban": "豆瓣",
    "tmdb": "TMDB",
    "tmdb_season": "TMDB",
    "plugin": "插件",
}

_METADATA_PROVIDER_OPTIONS: list[tuple[str, str]] = [
    ("tencent", "腾讯"),
    ("official_douban", "豆瓣官方"),
    ("tmdb", "TMDB"),
    ("local_douban", "本地豆瓣"),
    ("douban", "豆瓣"),
    ("plugin", "插件"),
]

_METADATA_SCRAPE_CATEGORY_OPTIONS: list[tuple[str, str]] = [
    ("", "自动"),
    ("电影", "电影"),
    ("剧集", "剧集"),
    ("动漫", "动漫"),
]

_METADATA_CHANGE_FIELDS: list[tuple[str, str, str]] = [
    ("poster", "vod_pic", "海报"),
    ("year", "vod_year", "年份"),
    ("genres", "type_name", "类型"),
    ("country", "vod_area", "地区"),
    ("language", "vod_lang", "语言"),
    ("directors", "vod_director", "导演"),
    ("actors", "vod_actor", "演员"),
    ("overview", "vod_content", "简介"),
    ("rating", "vod_remarks", "评分"),
    ("douban_id", "dbid", "豆瓣ID"),
]

_INLINE_METADATA_CR_RE = re.compile(r"\[a=cr:(?P<payload>\{.*?\})/\](?P<label>.*?)\[/a\]", re.DOTALL)
_BILIBILI_BVID_RE = re.compile(r"^BV[0-9A-Za-z]+$")
_BILIBILI_SS_ID_RE = re.compile(r"^ss(\d+)$", re.IGNORECASE)
_BILIBILI_SEASON_ID_RE = re.compile(r"^season\$(\d+)$", re.IGNORECASE)
_BILIBILI_IDENTITY_DETAIL_LABELS = {"bvid", "season id"}
_HIDDEN_METADATA_DETAIL_LABELS = {"number_of_episodes", "number_of_seasons"}
logger = logging.getLogger(__name__)


def _is_backend_proxy_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 3 or segments[0] != "p" or not segments[1] or not segments[2]:
        return False
    payload_segments = segments[2].split("@")
    if len(payload_segments) < 2:
        return False
    site_id = payload_segments[0].strip()
    proxy_id = payload_segments[1].strip()
    return site_id.isdigit() and bool(proxy_id)


def _summarize_media_url(url: str) -> str:
    if url.startswith("data:application/dash+xml;base64,"):
        return "data:application/dash+xml;base64,..."
    parsed = urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return url
    path = parsed.path or "/"
    if len(path) > 96:
        path = f"...{path[-96:]}"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _ytdlp_quality_height(quality_id: str) -> int | None:
    if not quality_id.startswith("ytdlp_"):
        return None
    suffix = quality_id.removeprefix("ytdlp_")
    if not suffix.isdigit():
        return None
    height = int(suffix)
    return height if height > 0 else None


def _metadata_provider_label(provider: str) -> str:
    normalized = str(provider or "").strip()
    if not normalized:
        return "未知来源"
    return _METADATA_PROVIDER_LABELS.get(normalized, normalized)


def _build_metadata_update_log(previous_vod: VodItem, updated_vod: VodItem) -> str:
    provider_changes: dict[str, list[str]] = {}
    provider_order: list[str] = []

    def add_change(provider: str, label: str) -> None:
        normalized_provider = str(provider or "").strip()
        if normalized_provider not in provider_changes:
            provider_changes[normalized_provider] = []
            provider_order.append(normalized_provider)
        if label not in provider_changes[normalized_provider]:
            provider_changes[normalized_provider].append(label)

    for field_key, attr_name, label in _METADATA_CHANGE_FIELDS:
        if getattr(previous_vod, attr_name) == getattr(updated_vod, attr_name):
            continue
        add_change(updated_vod.metadata_field_sources.get(field_key, ""), label)

    if previous_vod.detail_fields != updated_vod.detail_fields:
        add_change(updated_vod.metadata_field_sources.get("detail_fields", ""), "扩展字段")

    if not provider_order:
        return ""

    parts = [
        f"{_metadata_provider_label(provider)}({ ' / '.join(provider_changes[provider]) })"
        for provider in provider_order
    ]
    return f"元数据已更新: {', '.join(parts)}"


def _build_metadata_hydration_query_log(query: MetadataQuery) -> str:
    return (
        f"元数据增强: {str(query.title or '').strip()} "
        f"年代={str(query.year or '').strip()} "
        f"分类={str(query.category_name or '').strip() or '自动'}"
    )


class ClickableSlider(QSlider):
    """A QSlider that allows clicking on the groove to set position directly."""

    clicked_value = Signal(int)

    _CHAPTER_GAP_PX = 2.0

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self._hover_tooltip_formatter: Callable[[int], str] | None = None
        self._buffer_value: int = 0
        self._chapter_positions: list[float] = []

    def paintEvent(self, event) -> None:
        if self.orientation() != Qt.Orientation.Horizontal:
            super().paintEvent(event)
            return

        tokens = current_theme_manager().player_tokens_for(current_resolved_theme())
        track_height = max(1, int(self.property("track_height") or 4))
        handle_diameter = max(1, int(self.property("handle_diameter") or 12))
        available_width = max(1, self.width() - handle_diameter)
        value_range = max(1, self.maximum() - self.minimum())
        progress = (self.value() - self.minimum()) / value_range
        handle_center_x = handle_diameter / 2 + progress * available_width
        track_top = (self.height() - track_height) / 2

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(tokens.player_overlay_bg))
        painter.setPen(Qt.PenStyle.NoPen)

        segments = self._chapter_segments(handle_diameter, available_width)
        if segments:
            clip_path = QPainterPath()
            for segment_start, segment_width in segments:
                clip_path.addRoundedRect(
                    segment_start,
                    track_top,
                    segment_width,
                    track_height,
                    track_height / 2,
                    track_height / 2,
                )
            painter.setClipPath(clip_path)

        track_color = tokens.player_button_border if self.isEnabled() else tokens.border_subtle
        painter.setBrush(QColor(track_color))
        painter.drawRoundedRect(0, track_top, self.width(), track_height, track_height / 2, track_height / 2)

        if self.isEnabled() and self._buffer_value > self.value():
            buffer_progress = (self._buffer_value - self.minimum()) / value_range
            buffer_end_x = handle_diameter / 2 + buffer_progress * available_width
            painter.setBrush(QColor(tokens.player_buffer))
            painter.drawRoundedRect(0, track_top, buffer_end_x, track_height, track_height / 2, track_height / 2)

        if self.isEnabled() and handle_center_x > handle_diameter / 2:
            painter.setBrush(QColor(tokens.accent))
            painter.drawRoundedRect(0, track_top, handle_center_x, track_height, track_height / 2, track_height / 2)

        if segments:
            painter.setClipping(False)

        handle_color = tokens.player_text_on_dark
        if not self.isEnabled():
            handle_color = tokens.text_secondary
        elif self.isSliderDown():
            handle_color = tokens.accent_hover
        elif self.underMouse():
            handle_color = tokens.accent
        painter.setBrush(QColor(handle_color))
        handle_top = (self.height() - handle_diameter) / 2
        painter.drawEllipse(handle_center_x - handle_diameter / 2, handle_top, handle_diameter, handle_diameter)
        painter.end()

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

    def set_buffer_value(self, value: int) -> None:
        clamped = max(self.minimum(), min(int(value), self.maximum()))
        if clamped == self._buffer_value:
            return
        self._buffer_value = clamped
        self.update()

    def set_hover_tooltip_formatter(self, formatter: Callable[[int], str] | None) -> None:
        self._hover_tooltip_formatter = formatter
        self.setMouseTracking(formatter is not None)

    def set_chapter_positions(self, positions: Iterable[float]) -> None:
        """Set chapter start offsets (in slider value units) used to segment the groove.

        Positions are kept unfiltered because the slider range is only known once the
        media duration has been observed; out-of-range values are dropped at paint time.
        """
        normalized: list[float] = []
        for raw_position in positions:
            try:
                position = float(raw_position)
            except (TypeError, ValueError):
                continue
            if position != position or position in (float("inf"), float("-inf")):
                continue
            normalized.append(position)
        normalized = sorted(set(normalized))
        if normalized == self._chapter_positions:
            return
        self._chapter_positions = normalized
        self.update()

    def _chapter_segments(
        self,
        handle_diameter: int,
        available_width: int,
    ) -> list[tuple[float, float]]:
        """Return (start_x, width) for each chapter segment, or [] when not segmented."""
        if not self._chapter_positions:
            return []
        minimum = self.minimum()
        maximum = self.maximum()
        if maximum <= minimum:
            return []
        value_range = max(1, maximum - minimum)
        boundaries: list[float] = []
        for position in self._chapter_positions:
            if position <= minimum or position >= maximum:
                continue
            offset = (position - minimum) / value_range * available_width
            boundary_x = handle_diameter / 2 + offset
            if boundaries and boundary_x - boundaries[-1] < self._CHAPTER_GAP_PX * 2:
                continue
            boundaries.append(boundary_x)
        if not boundaries:
            return []

        segments: list[tuple[float, float]] = []
        edges = [0.0, *boundaries, float(self.width())]
        for index, (start_x, end_x) in enumerate(zip(edges, edges[1:], strict=False)):
            is_last = index == len(edges) - 2
            segment_width = end_x - start_x - (0.0 if is_last else self._CHAPTER_GAP_PX)
            if segment_width <= 0:
                continue
            segments.append((start_x, segment_width))
        return segments if len(segments) > 1 else []

    def _pixel_pos_to_value(self, pos: int) -> int:
        groove_rect = self.rect()
        handle_width = max(1, int(self.property("handle_diameter") or 12))
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


class ClickableLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class PosterPreviewDialog(ThemedDialogBase):
    def __init__(self, pixmap: QPixmap, *, parent: QWidget | None = None) -> None:
        super().__init__(title="封面预览", parent=parent, allow_maximize=False, resizable=True)
        self._source_pixmap = pixmap
        self.content_layout().setContentsMargins(4, 4, 4, 4)
        self.content_layout().setSpacing(0)
        self.previous_button = QToolButton(self)
        self.previous_button.setToolTip("上一张海报")
        self.previous_button.setArrowType(Qt.ArrowType.LeftArrow)
        self.previous_button.setAutoRaise(True)
        self.previous_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.previous_button.setFixedSize(32, 32)
        self.next_button = QToolButton(self)
        self.next_button.setToolTip("下一张海报")
        self.next_button.setArrowType(Qt.ArrowType.RightArrow)
        self.next_button.setAutoRaise(True)
        self.next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_button.setFixedSize(32, 32)
        self.preview_label = QLabel(self)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(900, 1040)
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        preview_row = QWidget(self)
        preview_layout = QHBoxLayout(preview_row)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(4)
        preview_layout.addWidget(self.previous_button, 0, Qt.AlignmentFlag.AlignVCenter)
        preview_layout.addWidget(self.preview_label, 1)
        preview_layout.addWidget(self.next_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.content_layout().addWidget(preview_row, 1)
        self.resize(980, 1120)
        self._render_preview()

    def set_source_pixmap(self, pixmap: QPixmap) -> None:
        self._source_pixmap = pixmap
        self._render_preview()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._render_preview()

    def _render_preview(self) -> None:
        if self._source_pixmap.isNull():
            self.preview_label.setPixmap(QPixmap())
            return
        target_size = self.preview_label.size()
        if target_size.width() <= 0 or target_size.height() <= 0:
            target_size = QSize(600, 700)
        self.preview_label.setPixmap(
            self._source_pixmap.scaled(
                target_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


class _PosterLoadSignals(QObject):
    loaded = Signal(int, object)


class _PlayItemResolveSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)


class _BackgroundTaskSignals(QObject):
    failed = Signal(str)


class _DanmakuSourceTaskSignals(QObject):
    finished = Signal(object, bool)


class _DanmakuPlaybackLogSignals(QObject):
    log = Signal(str)


class _DanmakuRenderSignals(QObject):
    succeeded = Signal(int, str, int)
    failed = Signal(int, str)


class _ExternalSubtitleFetchSignals(QObject):
    succeeded = Signal(int, object, str)
    failed = Signal(int, object, str)


@dataclass(slots=True)
class _ExternalSubtitleFetchRequest:
    token: int
    subtitle: ExternalSubtitleOption
    secondary: bool
    purpose: str
    previous_track_id: int | None = None


class _PlaybackPrepareSignals(QObject):
    succeeded = Signal(int, str)
    failed = Signal(int, str)


class _PlaybackLoaderSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)


class _MetadataHydrationSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)


class _MetadataScrapeSignals(QObject):
    search_succeeded = Signal(int, object)
    apply_succeeded = Signal(int, object)
    failed = Signal(int, str)


class _EpisodeTitleEnhancementSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)


class _DetailActionSignals(QObject):
    succeeded = Signal(int, object, object)
    failed = Signal(int, str)


class _HeatSummarySignals(QObject):
    loaded = Signal(int, object)


class _SubtitleSearchSignals(QObject):
    search_succeeded = Signal(int, object)
    download_succeeded = Signal(int, object, object)
    failed = Signal(int, str)


# 结果表列序：来源 / 名称 / 语言 / 格式 / 匹配度
_SUBTITLE_SEARCH_COLUMNS = ("来源", "字幕", "语言", "格式", "匹配度")
_SUBTITLE_SEARCH_NAME_COLUMN = 1

# 语言筛选下拉项。默认不限制语言，仅靠匹配打分把简英双语排在最前，
# 用户想只看某种语言时再手动收窄。
_SUBTITLE_LANGUAGE_FILTERS = (
    ("", "全部（简英双语优先）"),
    ("chs_eng", "简英双语"),
    ("chs", "简体中文"),
    ("cht", "繁体中文"),
    ("eng", "English"),
)


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
class _MetadataScrapeApplyResult:
    updated_vod: VodItem
    candidate: object
    previous_vod: VodItem
    updated_playlist: list[PlayItem] | None = None
    binding_key: tuple[str, str] | None = None
    alias_binding_key: tuple[str, str] | None = None
    episode_title_error: str = ""
    binding_error: str = ""


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
    vod_snapshot: object | None = None
    intent_generation: int = 0


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
    intent_generation: int = 0


@dataclass(slots=True)
class _PendingPlaybackLoader:
    index: int
    previous_index: int
    start_position_seconds: int
    pause: bool
    hydrate_only: bool = False
    youtube_detail_parse: bool = False
    playback_started_url: str = ""
    playback_started_audio_url: str = ""
    playback_started_headers: dict[str, str] | None = None
    playback_started_quality_id: str = ""
    intent_generation: int = 0


class _PlayerToolDialog(ThemedDialogBase):
    def __init__(self, *, title: str, parent: QWidget, size: tuple[int, int]) -> None:
        super().__init__(title=title, parent=parent)
        self.resize(*size)


class PlayerWindow(ThemedWidgetWindowBase, AsyncGuardMixin):
    _PSEUDO_MAXIMIZED_GEOMETRY_PREFIX = b"ATV_PLAYER_PSEUDO_MAXIMIZED_V1\0"
    _PSEUDO_MAXIMIZED_GEOMETRY_RECT_SIZE = struct.calcsize(">4i")
    _DASH_DATA_URI_PREFIX = "data:application/dash+xml;base64,"
    closed_to_main = Signal()
    global_search_requested = Signal(str)
    _SEEK_SHORTCUT_SECONDS = 15
    _MODIFIED_SEEK_SHORTCUT_SECONDS = 60
    _VOLUME_SHORTCUT_STEP = 5
    _PICTURE_ADJUSTMENT_PROPS: tuple[tuple[str, str], ...] = (
        ("brightness", "亮度"),
        ("contrast", "对比度"),
        ("saturation", "饱和度"),
        ("hue", "色调"),
        ("gamma", "伽马"),
    )
    _CURSOR_HIDE_DELAY_MS = 2000
    _MANUAL_SUBTITLE_SWITCH_REFRESH_WINDOW_SECONDS = 1.0
    _VIDEO_CONTEXT_MENU_DUPLICATE_WINDOW_MS = 250
    _VIDEO_CONTEXT_MENU_DUPLICATE_DISTANCE = 8
    _POSTER_SIZE = QSize(180, 260)
    _VIDEO_POSTER_LOAD_FALLBACK_SIZE = QSize(960, 540)
    _DETAIL_LOG_MAX_HEIGHT_DIVISOR = 4
    _POSTER_REQUEST_TIMEOUT_SECONDS = 10.0
    _COMPACT_CONTROLS_WIDTH_THRESHOLD = 1120
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
    _DEFAULT_MAIN_SPLITTER_SIZES = [960, 320]
    _DANMAKU_SECONDARY_SCALE = 100
    _FAVORITE_ACTIVE_ICON_COLOR = "#e53935"
    _FOLLOWING_ACTIVE_ICON_COLOR = "#ff6a3d"
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
    _TINTED_ICON_NAMES = {
        "favorite.svg",
        "favorite-filled.svg",
        "grid.svg",
        "maximize.svg",
        "queue.svg",
        "poster.svg",
        "info.svg",
        "logs.svg",
        "danmaku.svg",
        "sliders.svg",
        "scrape.svg",
    }

    def __init__(
        self,
        controller,
        config=None,
        save_config=None,
        app_log_service=None,
        m3u8_ad_filter=None,
        playback_parser_service=None,
        default_video_cover_loader=None,
        favorite_is_active=None,
        favorite_toggle=None,
        following_is_active=None,
        following_toggle=None,
        following_progress_reporter=None,
        heat_controller=None,
    ) -> None:
        super().__init__(
            title="alist-tvbox 播放器",
            allow_minimize=True,
            allow_maximize=True,
            resizable=True,
        )
        self._init_async_guard()
        self.controller = controller
        self.config = config
        self._save_config = save_config or (lambda: None)
        self._app_log_service = app_log_service
        self._m3u8_ad_filter = m3u8_ad_filter or M3U8AdFilter()
        self._playback_parser_service = playback_parser_service
        self._startup_coordinator = PlaybackStartupCoordinator()
        self._startup_state = self._startup_coordinator.idle()
        self._default_video_cover_loader = default_video_cover_loader
        self._favorite_is_active = favorite_is_active or (lambda _item: False)
        self._favorite_toggle = favorite_toggle or (lambda _item: None)
        self._following_is_active = following_is_active or (lambda _item: False)
        self._following_toggle = following_toggle or (lambda _item: None)
        self._following_progress_reporter = following_progress_reporter or (
            lambda *_args, **_kwargs: None
        )
        self._heat_controller = heat_controller
        self._heat_summary_request_id = 0
        self._heat_summary_text = ""
        self._default_video_cover_source: str | None = None
        self.session = None
        self.current_index = 0
        self.current_speed = 1.0
        self.is_playing = True
        self._playback_intent_generation = 0
        self._is_muted = bool(getattr(self.config, "player_muted", False))
        self._subtitle_delay = 0.0
        self._audio_delay = 0.0
        self._picture_adjustments: dict[str, int] = {
            "brightness": 0,
            "contrast": 0,
            "saturation": 0,
            "hue": 0,
            "gamma": 0,
        }
        self._was_maximized_before_fullscreen = False
        self._quit_requested = False
        self._app_quit_requested = False
        self._close_event_returns_to_main = False
        self._always_on_top_enabled = False
        self._always_on_top_applied = False
        self._pseudo_maximized = False
        self._normal_geometry_before_pseudo_maximize: QRect | None = None
        self._normal_window_state_before_pseudo_maximize: bytes | None = None
        self._restore_pseudo_maximized_on_show = False
        self._restored_pseudo_normal_geometry: QRect | None = None
        self._always_on_top_reapply_pending = False
        self._video_pointer_inside = False
        self._app_event_filter_installed = False
        self._last_cursor_pos = None
        self._last_cursor_activity_ms = 0
        self._poster_request_id = 0
        self._video_poster_request_id = 0
        self._play_item_request_id = 0
        self._playback_loader_request_id = 0
        self._metadata_request_id = 0
        self._metadata_scrape_request_id = 0
        self._subtitle_search_request_id = 0
        self._episode_title_request_id = 0
        self._playback_prepare_request_id = 0
        self._detail_action_request_id = 0
        self._bilibili_tree_flat_index_by_item_id: dict[int, int] = {}
        self._bilibili_tree_flat_index_by_group_item: dict[tuple[int, int], int] = {}
        self._bilibili_tree_group_item_by_flat_index: dict[int, tuple[int, int]] = {}
        self._restore_saved_splitter_on_next_wide_exit = False
        self._pending_play_item_load: _PendingPlayItemLoad | None = None
        self._pending_playback_loader: _PendingPlaybackLoader | None = None
        self._pending_metadata_session = None
        self._pending_episode_title_session = None
        self._pending_playback_prepare: _PendingPlaybackPrepare | None = None
        self._video_context_menu: QMenu | None = None
        self._always_on_top_menu_action: QAction | None = None
        self._poster_preview_dialog: QDialog | None = None
        self._poster_preview_label: QLabel | None = None
        self._poster_preview_previous_button: QToolButton | None = None
        self._poster_preview_next_button: QToolButton | None = None
        self._danmaku_source_dialog: QDialog | None = None
        self._danmaku_settings_dialog: QDialog | None = None
        self._metadata_scrape_dialog: QDialog | None = None
        self._subtitle_search_dialog: QDialog | None = None
        self._subtitle_search_context_label: QLabel | None = None
        self._subtitle_search_title_edit: QLineEdit | None = None
        self._subtitle_search_language_combo: QComboBox | None = None
        self._subtitle_search_provider_combo: QComboBox | None = None
        self._subtitle_search_tmdb_id_edit: QLineEdit | None = None
        self._subtitle_search_imdb_id_edit: QLineEdit | None = None
        self._subtitle_search_table: QTableWidget | None = None
        self._subtitle_search_status_label: QLabel | None = None
        self._subtitle_search_button: QPushButton | None = None
        self._subtitle_search_apply_button: QPushButton | None = None
        self._subtitle_search_secondary_button: QPushButton | None = None
        self._subtitle_search_items: list[object] = []
        self._subtitle_search_result: object | None = None
        # 记录上次搜索对应的播放上下文和自动填充值，切集/换片后用来识别并重置过期内容
        self._subtitle_search_last_context: tuple[str, str, int | None] | None = None
        self._subtitle_search_auto_title = ""
        self._subtitle_search_auto_tmdb_id = ""
        self._danmaku_source_title_edit: QLineEdit | None = None
        self._danmaku_source_episode_edit: QLineEdit | None = None
        self._danmaku_source_url_edit: QLineEdit | None = None
        self._danmaku_source_url_download_button: QPushButton | None = None
        self._danmaku_source_search_provider_combo: QComboBox | None = None
        self._danmaku_source_status_label: QLabel | None = None
        self._danmaku_source_provider_list: QListWidget | None = None
        self._danmaku_source_option_list: QListWidget | None = None
        self._danmaku_source_rerun_button: QPushButton | None = None
        self._danmaku_source_clear_button: QPushButton | None = None
        self._danmaku_source_switch_button: QPushButton | None = None
        self._danmaku_source_offset_spin: QDoubleSpinBox | None = None
        self._danmaku_source_offset_reset_button: QPushButton | None = None
        self._pending_danmaku_offset_item: PlayItem | None = None
        self._metadata_scrape_title_edit: QLineEdit | None = None
        self._metadata_scrape_year_edit: QLineEdit | None = None
        self._metadata_scrape_category_combo: QComboBox | None = None
        self._metadata_scrape_provider_combo: QComboBox | None = None
        self._metadata_scrape_group_list: QListWidget | None = None
        self._metadata_scrape_result_list: QListWidget | None = None
        self._metadata_scrape_status_label: QLabel | None = None
        self._metadata_scrape_rerun_button: QPushButton | None = None
        self._metadata_scrape_reset_button: QPushButton | None = None
        self._metadata_scrape_restore_query_button: QPushButton | None = None
        self._metadata_scrape_apply_button: QPushButton | None = None
        self._metadata_scrape_groups: list[object] = []
        self._metadata_scrape_default_title = ""
        self._metadata_scrape_default_year = ""
        self._metadata_scrape_binding_title = ""
        self._metadata_scrape_binding_year = ""
        self._metadata_scrape_alias_binding_key: tuple[str, str] | None = None
        self._metadata_scrape_query_saved = False
        self._metadata_scrape_saved_title = ""
        self._metadata_scrape_saved_year = ""
        self._metadata_scrape_saved_category = ""
        self._metadata_scrape_saved_provider = ""
        self._metadata_hydration_override_title = ""
        self._metadata_hydration_override_year = ""
        self._metadata_hydration_override_category = ""
        self._restart_episode_title_after_next_metadata_hydration = False
        self._force_episode_title_restart_on_metadata_request_id = 0
        self._danmaku_research_pending = False
        self._danmaku_last_searched_query = ""
        self._danmaku_render_mode_combo: QComboBox | None = None
        self._danmaku_color_mode_combo: QComboBox | None = None
        self._danmaku_uniform_color_edit: QLineEdit | None = None
        self._danmaku_uniform_color_button: QPushButton | None = None
        self._danmaku_uniform_color_dialog: QColorDialog | None = None
        self._danmaku_uniform_color_preview_original: str | None = None
        self._danmaku_line_count_spin: QSpinBox | None = None
        self._danmaku_font_size_spin: QSpinBox | None = None
        self._danmaku_opacity_spin: QSpinBox | None = None
        self._danmaku_scroll_speed_spin: QDoubleSpinBox | None = None
        self._danmaku_position_preset_combo: QComboBox | None = None
        self._danmaku_outline_strength_combo: QComboBox | None = None
        self._last_video_context_menu_request_ms = 0
        self._last_video_context_menu_request_global_pos: tuple[int, int] | None = None
        self._video_surface_ready = False
        self._video_picture_state = "idle"
        self._auto_advance_locked = False
        self._observed_media_duration_seconds = 0
        self._current_chapters: list[Chapter] = []
        self._last_playback_position_seconds = 0
        self._premature_finish_recovery_attempts = 0
        self._ignore_playback_finished_until = 0.0
        self._recent_user_seek_target_seconds: int | None = None
        self._auto_switched_failure_sources: set[tuple[int, int]] = set()
        self._ytdlp_full_resolve_recovery_item: PlayItem | None = None
        self._danmaku_track_id: int | None = None
        self._danmaku_temp_path: Path | None = None
        self._danmaku_temp_path_is_ephemeral = False
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
        self._active_danmaku_source_task_counts: dict[int, int] = {}
        self._danmaku_source_task_pending_state: dict[int, bool] = {}
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
        self._metadata_hydration_signals = _MetadataHydrationSignals()
        self._connect_async_signal(self._metadata_hydration_signals.succeeded, self._handle_metadata_hydration_succeeded)
        self._connect_async_signal(self._metadata_hydration_signals.failed, self._handle_metadata_hydration_failed)
        self._metadata_scrape_signals = _MetadataScrapeSignals()
        self._subtitle_search_signals = _SubtitleSearchSignals()
        self._subtitle_search_signals.search_succeeded.connect(
            self._handle_subtitle_search_succeeded
        )
        self._subtitle_search_signals.download_succeeded.connect(
            self._handle_subtitle_download_succeeded
        )
        self._subtitle_search_signals.failed.connect(self._handle_subtitle_search_failed)
        self._connect_async_signal(
            self._metadata_scrape_signals.search_succeeded,
            self._handle_metadata_scrape_search_succeeded,
        )
        self._connect_async_signal(
            self._metadata_scrape_signals.apply_succeeded,
            self._handle_metadata_scrape_apply_succeeded,
        )
        self._connect_async_signal(self._metadata_scrape_signals.failed, self._handle_metadata_scrape_failed)
        self._episode_title_enhancement_signals = _EpisodeTitleEnhancementSignals()
        self._connect_async_signal(
            self._episode_title_enhancement_signals.succeeded,
            self._handle_episode_title_enhancement_succeeded,
        )
        self._connect_async_signal(
            self._episode_title_enhancement_signals.failed,
            self._handle_episode_title_enhancement_failed,
        )
        self._detail_action_signals = _DetailActionSignals()
        self._connect_async_signal(self._detail_action_signals.succeeded, self._handle_detail_action_succeeded)
        self._connect_async_signal(self._detail_action_signals.failed, self._handle_detail_action_failed)
        self._heat_summary_signals = _HeatSummarySignals()
        self._connect_async_signal(self._heat_summary_signals.loaded, self._handle_heat_summary_loaded)
        self._playback_prepare_signals = _PlaybackPrepareSignals()
        self._connect_async_signal(self._playback_prepare_signals.succeeded, self._handle_playback_prepare_succeeded)
        self._connect_async_signal(self._playback_prepare_signals.failed, self._handle_playback_prepare_failed)
        self._background_task_signals = _BackgroundTaskSignals()
        self._connect_async_signal(self._background_task_signals.failed, self._append_log)
        self._danmaku_source_task_signals = _DanmakuSourceTaskSignals()
        self._connect_async_signal(self._danmaku_source_task_signals.finished, self._handle_danmaku_source_task_finished)
        self._danmaku_playback_log_signals = _DanmakuPlaybackLogSignals()
        self._connect_async_signal(self._danmaku_playback_log_signals.log, self._append_log)
        self._danmaku_render_signals = _DanmakuRenderSignals()
        self._connect_async_signal(self._danmaku_render_signals.succeeded, self._handle_danmaku_render_succeeded)
        self._connect_async_signal(self._danmaku_render_signals.failed, self._handle_danmaku_render_failed)
        self._external_subtitle_fetch_signals = _ExternalSubtitleFetchSignals()
        self._connect_async_signal(
            self._external_subtitle_fetch_signals.succeeded,
            self._handle_external_subtitle_fetch_succeeded,
        )
        self._connect_async_signal(self._external_subtitle_fetch_signals.failed, self._handle_external_subtitle_fetch_failed)
        self._danmaku_retry_timer = QTimer(self)
        self._danmaku_retry_timer.setSingleShot(True)
        self._danmaku_retry_timer.timeout.connect(self._retry_configure_danmaku_for_current_item)
        self._danmaku_offset_save_timer = QTimer(self)
        self._danmaku_offset_save_timer.setSingleShot(True)
        self._danmaku_offset_save_timer.setInterval(250)
        self._danmaku_offset_save_timer.timeout.connect(self._apply_pending_danmaku_offset)
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
        self.setMinimumSize(800, 600)
        self._icons_dir = Path(__file__).resolve().parent.parent / "icons"

        self.video_widget = MpvWidget(self, config=self.config)
        self._configure_video_surface_widgets()
        self.video_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.video_widget.customContextMenuRequested.connect(self._show_video_context_menu)
        self.video_widget.context_menu_requested.connect(self._show_video_context_menu_at_cursor)
        self.video_widget.context_menu_dismiss_requested.connect(self._dismiss_video_context_menu_at_cursor)
        self.video_widget.left_clicked.connect(self._release_focus_for_video_press)
        self.video_widget.playback_failed.connect(self._handle_playback_failed)
        self.video_widget.file_loaded.connect(self._handle_video_file_loaded)
        self.video_widget.video_picture_state_changed.connect(self._handle_video_picture_state_changed)
        self.video_widget.pause_state_changed.connect(self._handle_pause_state_changed)
        self.video = self.video_widget
        self.controls = PlayerControls(self)
        self.controls.bind(config=self.config)
        self._pending_post_load_item: PlayItem | None = None
        self._pending_post_load_pause = False
        self._pending_file_loaded_danmaku_item: PlayItem | None = None
        self._pending_ytdlp_metadata_hydration: tuple[PlayItem, int] | None = None
        self._danmaku_render_request_id = 0
        self._pending_danmaku_render_item: PlayItem | None = None
        self.always_on_top_button = QPushButton("", self.title_bar())
        self.always_on_top_button.setObjectName("customTitleBarAlwaysOnTopButton")
        self.always_on_top_button.setCheckable(True)
        self.always_on_top_button.setIconSize(QSize(16, 16))
        self.always_on_top_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.always_on_top_button.toggled.connect(self._set_always_on_top)
        self.title_bar_return_button = QPushButton("", self.title_bar())
        self.title_bar_return_button.setObjectName("customTitleBarReturnButton")
        self.title_bar_return_button.setProperty("icon_name", "home.svg")
        self.title_bar_return_button.setIcon(load_icon(self._icons_dir / "home.svg"))
        self.title_bar_return_button.setIconSize(QSize(16, 16))
        self.title_bar_return_button.setToolTip(self._format_tooltip("返回主窗口", "Ctrl+P"))
        self.title_bar_return_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title_bar_return_button.clicked.connect(self._return_to_main)
        self.title_bar().set_extra_action_buttons(
            [self.always_on_top_button, self.title_bar_return_button]
        )
        self._sync_always_on_top_controls()
        self.title_bar().close_requested.disconnect()
        self.title_bar().close_requested.connect(self._quit_application)
        self.playlist_title_mode = "episode"
        self._playlist_panel_mode = "list"
        self._playlist_panel_resume_mode = "list"
        self.playlist_group_combo = FlatComboBox()
        self.playlist_group_combo.setHidden(True)
        self.playlist_source_combo = FlatComboBox()
        self.playlist_source_combo.setHidden(True)
        self.playlist_subgroup_combo = FlatComboBox()
        self.playlist_subgroup_combo.setHidden(True)
        self.playlist_sort_combo = FlatComboBox()
        self.playlist_sort_combo.setHidden(True)
        self._playlist_sort_state = PlaylistSortState()
        self.playlist_title_tabs = QTabBar()
        self.playlist_title_tabs.addTab("剧集标题")
        self.playlist_title_tabs.addTab("原始文件名")
        self.playlist_title_tabs.setCursor(Qt.CursorShape.PointingHandCursor)
        self.playlist_title_tabs.setHidden(True)
        self.playlist = QListWidget()
        self.playlist.setSpacing(1)
        self.playlist.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.playlist.customContextMenuRequested.connect(lambda pos: self._show_playlist_context_menu(pos))
        self.playlist.viewport().installEventFilter(self)
        self.bilibili_playlist_tree = QTreeWidget()
        self.bilibili_playlist_tree.setHeaderHidden(True)
        self.bilibili_playlist_tree.setIndentation(14)
        self.bilibili_playlist_tree.setHidden(True)
        self.bilibili_playlist_tree.itemClicked.connect(self._handle_bilibili_tree_item_clicked)
        self.bilibili_playlist_tree.itemDoubleClicked.connect(self._handle_bilibili_tree_item_activated)
        self.play_button = self._create_icon_button("play.svg", "播放/暂停", "Space", role="primary")
        self.prev_button = self._create_icon_button("previous.svg", "上一集", "PgUp", role="secondary")
        self.next_button = self._create_icon_button("next.svg", "下一集", "PgDn", role="secondary")
        self.backward_button = self._create_icon_button("seek-backward.svg", "后退", "Left", role="secondary")
        self.forward_button = self._create_icon_button("seek-forward.svg", "前进", "Right", role="secondary")
        self.refresh_button = self._create_icon_button("refresh.svg", "重新播放", role="secondary")
        self.mute_button = self._create_icon_button("volume-on.svg", "静音", "M", role="secondary")
        self.wide_button = self._create_icon_button("grid.svg", "宽屏", "W", role="secondary")
        self.fullscreen_button = self._create_icon_button("maximize.svg", "全屏", "Enter", role="secondary")
        self.wide_button.setCheckable(True)
        if self.config is not None:
            self.wide_button.setChecked(bool(self.config.player_wide_mode))
        self.toggle_playlist_button = self._create_icon_button("queue.svg", "播放列表", role="secondary")
        self.toggle_poster_button = self._create_icon_button("poster.svg", "海报", role="secondary")
        self.toggle_details_button = self._create_icon_button("info.svg", "详情", role="secondary")
        self.toggle_log_button = self._create_icon_button("logs.svg", "播放日志", role="secondary")
        self.danmaku_source_button = self._create_icon_button("danmaku.svg", "弹幕源", "D", role="secondary")
        self.danmaku_settings_button = self._create_icon_button("sliders.svg", "弹幕设置", "Ctrl+D", role="secondary")
        self.metadata_scrape_button = self._create_icon_button("scrape.svg", "刮削", "S", role="secondary")
        self.toggle_playlist_button.setCheckable(True)
        self.toggle_poster_button.setCheckable(True)
        self.toggle_details_button.setCheckable(True)
        self.toggle_log_button.setCheckable(True)
        self.toggle_poster_button.setChecked(True)
        self.toggle_details_button.setChecked(True)
        self.toggle_log_button.setChecked(bool(getattr(self.config, "player_log_visible", True)))
        self._update_playlist_toggle_button_state()

        self.speed_combo = FlatComboBox()
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
        self._external_subtitle_fetch_counter = 0
        self._primary_external_subtitle_fetch: _ExternalSubtitleFetchRequest | None = None
        self._secondary_external_subtitle_fetch: _ExternalSubtitleFetchRequest | None = None
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
        self.subtitle_combo = FlatComboBox()
        self.subtitle_combo.addItem("字幕", ("auto", None))
        self.subtitle_combo.setEnabled(False)
        self.danmaku_combo = FlatComboBox()
        self._reset_danmaku_combo()
        self._video_quality_options: list[VideoQualityOption] = []
        self.video_quality_combo = FlatComboBox()
        self._reset_video_quality_combo()
        self._audio_tracks: list[AudioTrack] = []
        self._audio_preference = AudioPreference()
        self.audio_combo = FlatComboBox()
        self.audio_combo.addItem("音轨", ("auto", None))
        self.audio_combo.setEnabled(False)
        self.parse_combo = FlatComboBox()
        self._width_adaptive_control_combos: list[QComboBox] = [
            self.subtitle_combo,
            self.danmaku_combo,
            self.video_quality_combo,
            self.audio_combo,
            self.parse_combo,
        ]
        self._configure_control_combo(self.playlist_group_combo, minimum_contents_length=10)
        self._configure_control_combo(self.playlist_source_combo, minimum_contents_length=12)
        self._configure_control_combo(self.playlist_subgroup_combo, minimum_contents_length=12)
        self._configure_control_combo(self.playlist_sort_combo, minimum_contents_length=10)
        self._configure_control_combo(self.speed_combo, minimum_contents_length=3, maximum_width=72, fixed_height=28)
        self._configure_control_combo(self.subtitle_combo, minimum_contents_length=2, maximum_width=74, fixed_height=28)
        self._configure_control_combo(self.danmaku_combo, minimum_contents_length=2, maximum_width=72, fixed_height=28)
        self._configure_control_combo(self.video_quality_combo, minimum_contents_length=3, maximum_width=84, fixed_height=28)
        self._configure_control_combo(self.audio_combo, minimum_contents_length=2, maximum_width=74, fixed_height=28)
        self._configure_control_combo(self.parse_combo, minimum_contents_length=2, maximum_width=72, fixed_height=28)
        self.opening_spin = self._create_skip_spinbox("片头 ")
        self.ending_spin = self._create_skip_spinbox("片尾 ")

        self.current_time_label = QLabel("00:00")
        self.current_time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.duration_label = QLabel("00:00")
        self.duration_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress = ClickableSlider(Qt.Orientation.Horizontal)
        self.progress.set_hover_tooltip_formatter(self._format_progress_tooltip)
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
        self.volume_value_label = QLabel()
        self.volume_value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.volume_value_label.setMinimumWidth(40)
        self._update_volume_value_label(initial_volume)
        self.poster_label = ClickableLabel()
        self.poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster_label.setMinimumSize(self._POSTER_SIZE)
        self.poster_label.setMaximumSize(self._POSTER_SIZE)
        self.poster_label.setText("")
        self.poster_label.setToolTip("点击查看大图")
        self.poster_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.poster_label.clicked.connect(self._open_detail_poster_preview)
        self._poster_previous_button = QToolButton()
        self._poster_previous_button.setToolTip("上一张海报")
        self._poster_previous_button.setArrowType(Qt.ArrowType.LeftArrow)
        self._poster_previous_button.setAutoRaise(True)
        self._poster_previous_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._poster_previous_button.setFixedSize(24, 24)
        self._poster_previous_button.setHidden(True)
        self._poster_next_button = QToolButton()
        self._poster_next_button.setToolTip("下一张海报")
        self._poster_next_button.setArrowType(Qt.ArrowType.RightArrow)
        self._poster_next_button.setAutoRaise(True)
        self._poster_next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._poster_next_button.setFixedSize(24, 24)
        self._poster_next_button.setHidden(True)
        self.video_poster_overlay = QLabel()
        self.video_poster_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_poster_overlay.setText("")
        self.video_poster_overlay.hide()
        self.metadata_view = QTextBrowser()
        self.metadata_view.setReadOnly(True)
        self.metadata_view.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.metadata_view.setOpenLinks(False)
        self.metadata_view.document().setDocumentMargin(4)
        self.metadata_view.anchorClicked.connect(self._handle_metadata_link)
        self._metadata_original_toggle = ToggleSwitch(False)
        self._metadata_original_toggle.setHidden(True)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setDocumentMargin(4)
        self._last_log_message: str | None = None
        self.playback_startup_widget = QWidget(self)
        self.playback_startup_widget.setObjectName("playbackStartupWidget")
        self.playback_retry_button = QPushButton("重试", self.playback_startup_widget)
        self.playback_switch_line_button = QPushButton("换线路", self.playback_startup_widget)
        self.playback_switch_parser_button = QPushButton("换解析器", self.playback_startup_widget)
        startup_layout = QHBoxLayout(self.playback_startup_widget)
        startup_layout.setContentsMargins(0, 0, 0, 0)
        startup_layout.setSpacing(6)
        startup_layout.addWidget(self.playback_retry_button)
        startup_layout.addWidget(self.playback_switch_line_button)
        startup_layout.addWidget(self.playback_switch_parser_button)
        self.playback_startup_widget.hide()
        self.details = QWidget()
        details_layout = QVBoxLayout(self.details)
        self.details_layout = details_layout
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(12)
        details_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.metadata_section = QWidget()
        metadata_layout = QVBoxLayout(self.metadata_section)
        metadata_layout.setContentsMargins(0, 0, 0, 0)
        metadata_layout.setSpacing(10)
        metadata_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._poster_row_widget = QWidget()
        self._poster_row_layout = QHBoxLayout(self._poster_row_widget)
        self._poster_row_layout.setContentsMargins(0, 0, 0, 0)
        self._poster_row_layout.setSpacing(6)
        self._poster_row_layout.addStretch(1)
        self._poster_row_layout.addWidget(self._poster_previous_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._poster_row_layout.addWidget(self.poster_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self._poster_row_layout.addWidget(self._poster_next_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._poster_row_layout.addStretch(1)
        metadata_layout.addWidget(self._poster_row_widget)
        self.favorite_button = self._create_icon_button("favorite.svg", "加入收藏")
        self.favorite_button.clicked.connect(self._toggle_current_favorite)
        self.following_button = self._create_icon_button("following.svg", "加入追更")
        self.following_button.clicked.connect(self._toggle_current_following)
        self.detail_actions_widget = QWidget()
        self.detail_actions_layout = QHBoxLayout(self.detail_actions_widget)
        self.detail_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_actions_layout.setSpacing(8)
        metadata_layout.addWidget(self.detail_actions_widget)
        self.detail_fields_widget = QWidget()
        self.detail_fields_layout = QVBoxLayout(self.detail_fields_widget)
        self.detail_fields_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_fields_layout.setSpacing(6)
        metadata_layout.addWidget(self.detail_fields_widget)
        self.metadata_heading = QLabel("影片详情")
        self._metadata_heading_row = QHBoxLayout()
        self._metadata_heading_row.setContentsMargins(0, 0, 8, 0)
        self._metadata_heading_row.setSpacing(6)
        self._metadata_heading_row.addWidget(self.metadata_heading)
        self._metadata_heading_row.addWidget(
            self.favorite_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        self._metadata_heading_row.addWidget(
            self.following_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        self._metadata_heading_row.addStretch(1)
        self._metadata_heading_row.addWidget(self._metadata_original_toggle, 0, Qt.AlignmentFlag.AlignRight)
        metadata_layout.addLayout(self._metadata_heading_row)
        metadata_layout.addWidget(self.metadata_view, 3)
        details_layout.addWidget(self.metadata_section, 3)
        self.log_section = QWidget()
        log_layout = QVBoxLayout(self.log_section)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(8)
        self.log_heading = QLabel("播放日志")
        log_layout.addWidget(self.log_heading)
        log_layout.addWidget(self.log_view, 1)
        details_layout.addWidget(self.playback_startup_widget)
        details_layout.addWidget(self.log_section, 1)
        self.details.installEventFilter(self)

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
        sidebar_actions = QHBoxLayout(self.sidebar_actions_widget)
        sidebar_actions.setContentsMargins(0, 0, 0, 0)
        sidebar_actions.addWidget(self.toggle_playlist_button)
        sidebar_actions.addWidget(self.toggle_poster_button)
        sidebar_actions.addWidget(self.toggle_details_button)
        sidebar_actions.addWidget(self.toggle_log_button)

        self.bottom_area = QWidget()
        self.bottom_area.setObjectName("playerBottomArea")
        self.bottom_area.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.bottom_area.setMaximumHeight(88)
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
        control_group_layout.addWidget(self.metadata_scrape_button)
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
        self.volume_layout.addWidget(self.volume_value_label)
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

        self.playlist_panel = QWidget()
        self.playlist_panel_layout = QVBoxLayout(self.playlist_panel)
        self.playlist_panel_layout.setContentsMargins(0, 0, 0, 0)
        self.playlist_panel_layout.setSpacing(0)
        self.playlist_panel_layout.addWidget(self.playlist)
        self.playlist_panel_layout.addWidget(self.bilibili_playlist_tree)

        self.sidebar_splitter = QSplitter(Qt.Orientation.Vertical)
        self.sidebar_splitter.addWidget(self.playlist_panel)
        self.sidebar_splitter.addWidget(self.details)
        self.sidebar_splitter.setChildrenCollapsible(True)

        sidebar_layout = QVBoxLayout()
        self.sidebar_layout = sidebar_layout
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(10)
        sidebar_layout.addWidget(self.sidebar_actions_widget)
        sidebar_layout.addWidget(self.playlist_group_combo)
        sidebar_layout.addWidget(self.playlist_source_combo)
        sidebar_layout.addWidget(self.playlist_subgroup_combo)
        sidebar_layout.addWidget(self.playlist_sort_combo)
        sidebar_layout.addWidget(self.playlist_title_tabs)
        sidebar_layout.addWidget(self.sidebar_splitter)
        self.sidebar_container = QWidget()
        self.sidebar_container.setMinimumWidth(250)
        self.sidebar_container.setLayout(sidebar_layout)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.addWidget(video_container)
        self.main_splitter.addWidget(self.sidebar_container)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 1)
        self._restore_main_splitter_state()
        self._sidebar_sizes = self.main_splitter.sizes()
        self._sidebar_splitter_sizes = [1, 1]
        if self.wide_button.isChecked():
            self._restore_saved_splitter_on_next_wide_exit = bool(
                self.config is not None and self.config.player_main_splitter_state
            )
            self.main_splitter.setSizes([1, 0])

        layout = self.content_layout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.main_splitter, 1)
        layout.addWidget(self.bottom_area, 0)
        if self.config and self.config.player_window_geometry:
            self._restore_player_window_geometry(self.config.player_window_geometry)
            self._sidebar_sizes = self.main_splitter.sizes()

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
        self.volume_slider.valueChanged.connect(self._update_volume_value_label)
        self.volume_slider.valueChanged.connect(self._change_volume)
        self.playlist_group_combo.currentIndexChanged.connect(self._change_playlist_group)
        self.playlist_source_combo.currentIndexChanged.connect(self._change_playlist_source)
        self.playlist_subgroup_combo.currentIndexChanged.connect(self._change_playlist_subgroup)
        self.playlist_sort_combo.currentIndexChanged.connect(self._change_playlist_sort)
        self.playlist_title_tabs.currentChanged.connect(self._change_playlist_title_mode)
        self._metadata_original_toggle.toggled.connect(self._toggle_original_metadata_view)
        self._poster_previous_button.clicked.connect(lambda: self._step_metadata_poster(-1))
        self._poster_next_button.clicked.connect(lambda: self._step_metadata_poster(1))
        self.playlist.itemDoubleClicked.connect(self._play_clicked_item)
        self.toggle_playlist_button.clicked.connect(self._cycle_playlist_panel_mode)
        self.toggle_poster_button.clicked.connect(self._apply_visibility_state)
        self.toggle_details_button.clicked.connect(self._update_sidebar_visibility)
        self.toggle_log_button.clicked.connect(self._toggle_log_visibility)
        self.playback_retry_button.clicked.connect(self._retry_failed_startup)
        self.playback_switch_line_button.clicked.connect(self._switch_line_after_failure)
        self.playback_switch_parser_button.clicked.connect(self._switch_parser_after_failure)
        self.danmaku_source_button.clicked.connect(self._open_danmaku_source_dialog)
        self.danmaku_settings_button.clicked.connect(self._open_danmaku_settings_dialog)
        self.metadata_scrape_button.clicked.connect(self._open_metadata_scrape_dialog)
        self.video_widget.double_clicked.connect(self.toggle_fullscreen)
        self.video_widget.playback_finished.connect(self._handle_playback_finished)
        self.video_widget.subtitle_tracks_changed.connect(self._refresh_subtitle_state)
        self.video_widget.audio_tracks_changed.connect(self._refresh_audio_state)
        self.video_widget.chapters_changed.connect(self._refresh_chapter_markers)
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
        self._apply_theme()
        self._apply_visibility_state()
        self._refresh_width_adaptive_control_visibility()
        self._update_log_section_max_height()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._mark_app_quit_requested)
            app.installEventFilter(self)
            self._app_event_filter_installed = True

    def _mark_app_quit_requested(self) -> None:
        self._app_quit_requested = True

    def _has_other_visible_top_level_windows(self) -> bool:
        app = QApplication.instance()
        if app is None:
            return False
        for widget in app.topLevelWidgets():
            if widget is self:
                continue
            if widget.isVisible():
                return True
        return False

    def _apply_theme(self) -> None:
        manager = current_theme_manager()
        theme = current_resolved_theme()
        tokens = manager.tokens_for(theme)
        player_tokens = manager.player_tokens_for(theme)
        self.refresh_window_chrome()
        self.details.setStyleSheet(build_player_panel_qss(tokens))
        self.sidebar_container.setStyleSheet(build_player_panel_qss(tokens))
        self.playlist.setStyleSheet(build_player_list_qss(tokens))
        self.bilibili_playlist_tree.setStyleSheet(build_player_list_qss(tokens))
        self.playlist_title_tabs.setStyleSheet(build_compact_player_tabbar_qss(tokens))
        self.metadata_view.setStyleSheet(build_player_text_panel_qss(tokens, padding="12px 14px"))
        self.log_view.setStyleSheet(build_player_text_panel_qss(tokens, padding="10px 12px"))
        heading_qss = build_player_section_heading_qss(tokens)
        self.metadata_heading.setStyleSheet(heading_qss)
        self.log_heading.setStyleSheet(heading_qss)
        self.bottom_area.setStyleSheet(build_player_immersive_qss(player_tokens))
        poster_arrow_qss = (
            f"QToolButton {{ border: none; background: transparent; color: {tokens.text_secondary}; padding: 0; }}"
            f"QToolButton:hover {{ background-color: {tokens.panel_alt_bg}; border-radius: 12px; }}"
        )
        self._poster_previous_button.setStyleSheet(poster_arrow_qss)
        self._poster_next_button.setStyleSheet(poster_arrow_qss)
        self._apply_favorite_button_theme()
        sidebar_combo_qss = build_combobox_qss(tokens)
        sidebar_combo_popup_qss = build_combobox_popup_qss(
            background=tokens.menu_bg,
            text_color=tokens.text_primary,
            border_color=tokens.input_border,
            hover_bg=tokens.menu_hover_bg,
            selected_bg=tokens.menu_selected_bg,
        )
        combo_qss = build_combobox_qss(
            tokens,
            border_radius=12,
            min_height=28,
            horizontal_padding=4,
            indicator_padding=18,
            drop_down_width=16,
            field_bg="transparent",
            drop_down_bg="transparent",
            text_color=player_tokens.player_text_on_dark,
            hover_field_bg=player_tokens.player_button_bg,
            hover_drop_down_bg=player_tokens.player_button_bg,
            disabled_field_bg=player_tokens.player_button_pressed_bg,
            disabled_drop_down_bg=player_tokens.player_button_pressed_bg,
            disabled_text_color=player_tokens.player_button_border,
            border_color="transparent",
            hover_border_color="transparent",
            focus_border_color=tokens.accent,
            disabled_border_color="transparent",
            drop_down_border_left_color="transparent",
            disabled_drop_down_border_left_color="transparent",
        )
        combo_popup_qss = build_combobox_popup_qss(
            background=player_tokens.player_button_bg,
            text_color=player_tokens.player_text_on_dark,
            border_color=player_tokens.player_button_border,
            hover_bg=player_tokens.player_button_hover_bg,
            selected_bg=player_tokens.player_button_hover_bg,
        )
        dialog_combo_qss = build_form_combobox_qss(tokens)
        dialog_combo_popup_qss = build_combobox_popup_qss(
            background=tokens.menu_bg,
            text_color=tokens.text_primary,
            border_color=tokens.input_border,
            hover_bg=tokens.menu_hover_bg,
            selected_bg=tokens.menu_selected_bg,
        )
        dialog_line_edit_qss = build_form_line_edit_qss(tokens)
        dialog_spinbox_qss = build_form_spinbox_qss(tokens)
        compact_dialog_spinbox_qss = build_form_spinbox_qss(
            tokens,
            border_radius=10,
            min_height=30,
        )
        skip_spinbox_qss = build_player_spinbox_qss(player_tokens)
        for combo in self._sidebar_comboboxes():
            combo.setStyleSheet(sidebar_combo_qss)
            combo.view().setStyleSheet(sidebar_combo_popup_qss)
            configure_flat_combobox(
                combo,
                text_color=tokens.text_primary,
                disabled_text_color=tokens.text_secondary,
                arrow_color=tokens.text_secondary,
                disabled_arrow_color=tokens.border_subtle,
                border_color="transparent",
                field_bg=tokens.input_bg,
                hover_field_bg=tokens.input_bg,
                disabled_field_bg=tokens.panel_alt_bg,
                hover_border_color=tokens.input_hover_border,
                focus_border_color=tokens.input_focus_ring,
                disabled_border_color="transparent",
                height=34,
            )
        for combo in self._player_control_comboboxes():
            combo.setStyleSheet(combo_qss)
            combo.view().setStyleSheet(combo_popup_qss)
            configure_flat_combobox(
                combo,
                text_color=player_tokens.player_text_on_dark,
                disabled_text_color=player_tokens.player_button_border,
                arrow_color=tokens.text_secondary,
                disabled_arrow_color=player_tokens.player_button_border,
                border_color=player_tokens.player_button_border,
                field_bg="transparent",
                hover_field_bg=player_tokens.player_button_bg,
                disabled_field_bg=player_tokens.player_button_pressed_bg,
                hover_border_color="transparent",
                focus_border_color=tokens.accent,
                disabled_border_color="transparent",
                border_radius=12,
                left_padding=4,
                indicator_padding=18,
                drop_down_width=16,
                height=28,
            )
        for combo in self._dialog_comboboxes():
            combo.setStyleSheet(dialog_combo_qss)
            combo.view().setStyleSheet(dialog_combo_popup_qss)
            configure_form_flat_combobox(combo, tokens)
        for edit in self._dialog_line_edits():
            edit.setStyleSheet(dialog_line_edit_qss)
        for spinbox in self._dialog_spinboxes():
            spinbox.setStyleSheet(
                compact_dialog_spinbox_qss
                if spinbox is self._danmaku_source_offset_spin
                else dialog_spinbox_qss
            )
        self.opening_spin.setStyleSheet(skip_spinbox_qss)
        self.ending_spin.setStyleSheet(skip_spinbox_qss)
        self.progress.setProperty("track_height", 4)
        self.progress.setProperty("handle_diameter", 12)
        self.volume_slider.setProperty("track_height", 4)
        self.volume_slider.setProperty("handle_diameter", 10)
        self.progress.setStyleSheet(build_slider_qss(player_tokens, groove_height=4, handle_diameter=12))
        self.volume_slider.setStyleSheet(build_slider_qss(player_tokens, groove_height=4, handle_diameter=10))
        self._sync_playlist_item_styles()
        for button in self._control_buttons():
            role = str(button.property("control_role") or "secondary")
            border_radius = 20 if role == "primary" else 16
            button.setStyleSheet(build_player_control_button_qss(player_tokens, role=role, border_radius=border_radius))
            icon_name = str(button.property("icon_name") or "")
            if icon_name:
                self._set_button_icon(button, icon_name)
        for dialog in (
            self._danmaku_settings_dialog,
            self._metadata_scrape_dialog,
            self._danmaku_source_dialog,
            self.help_dialog,
        ):
            refresh_window_chrome = getattr(dialog, "refresh_window_chrome", None)
            if callable(refresh_window_chrome):
                refresh_window_chrome()

    def _control_buttons(self) -> list[QPushButton]:
        return [
            self.play_button,
            self.prev_button,
            self.next_button,
            self.backward_button,
            self.forward_button,
            self.refresh_button,
            self.mute_button,
            self.wide_button,
            self.fullscreen_button,
            self.toggle_playlist_button,
            self.toggle_poster_button,
            self.toggle_details_button,
            self.toggle_log_button,
            self.danmaku_source_button,
            self.danmaku_settings_button,
            self.metadata_scrape_button,
        ]

    def _sidebar_comboboxes(self) -> list[QComboBox]:
        return [
            self.playlist_group_combo,
            self.playlist_source_combo,
            self.playlist_subgroup_combo,
        ]

    def _player_control_comboboxes(self) -> list[QComboBox]:
        return [
            self.speed_combo,
            self.subtitle_combo,
            self.danmaku_combo,
            self.video_quality_combo,
            self.audio_combo,
            self.parse_combo,
        ]

    def _dialog_comboboxes(self) -> list[QComboBox]:
        combos: list[QComboBox] = []
        if self._metadata_scrape_category_combo is not None:
            combos.append(self._metadata_scrape_category_combo)
        if self._metadata_scrape_provider_combo is not None:
            combos.append(self._metadata_scrape_provider_combo)
        if self._danmaku_source_search_provider_combo is not None:
            combos.append(self._danmaku_source_search_provider_combo)
        if self._danmaku_render_mode_combo is not None:
            combos.append(self._danmaku_render_mode_combo)
        if self._danmaku_position_preset_combo is not None:
            combos.append(self._danmaku_position_preset_combo)
        if self._danmaku_color_mode_combo is not None:
            combos.append(self._danmaku_color_mode_combo)
        return combos

    def _dialog_line_edits(self) -> list[QLineEdit]:
        edits: list[QLineEdit] = []
        if self._metadata_scrape_title_edit is not None:
            edits.append(self._metadata_scrape_title_edit)
        if self._metadata_scrape_year_edit is not None:
            edits.append(self._metadata_scrape_year_edit)
        if self._danmaku_source_title_edit is not None:
            edits.append(self._danmaku_source_title_edit)
        if self._danmaku_source_episode_edit is not None:
            edits.append(self._danmaku_source_episode_edit)
        if self._danmaku_source_url_edit is not None:
            edits.append(self._danmaku_source_url_edit)
        return edits

    def _dialog_spinboxes(self) -> list[QWidget]:
        spinboxes: list[QWidget] = []
        if self._danmaku_line_count_spin is not None:
            spinboxes.append(self._danmaku_line_count_spin)
        if self._danmaku_font_size_spin is not None:
            spinboxes.append(self._danmaku_font_size_spin)
        if self._danmaku_opacity_spin is not None:
            spinboxes.append(self._danmaku_opacity_spin)
        if self._danmaku_scroll_speed_spin is not None:
            spinboxes.append(self._danmaku_scroll_speed_spin)
        if self._danmaku_source_offset_spin is not None:
            spinboxes.append(self._danmaku_source_offset_spin)
        return spinboxes

    def _set_fixed_control_height(self, widget: QWidget | None, height: int) -> None:
        if widget is None:
            return
        widget.setFixedHeight(height)

    def _refresh_metadata_scrape_search_row_heights(self) -> None:
        self._set_fixed_control_height(self._metadata_scrape_title_edit, 40)
        self._set_fixed_control_height(self._metadata_scrape_year_edit, 40)
        self._set_fixed_control_height(self._metadata_scrape_category_combo, 42)
        self._set_fixed_control_height(self._metadata_scrape_provider_combo, 42)

    def _refresh_danmaku_source_search_row_heights(self) -> None:
        self._set_fixed_control_height(self._danmaku_source_title_edit, 40)
        self._set_fixed_control_height(self._danmaku_source_episode_edit, 40)
        self._set_fixed_control_height(self._danmaku_source_url_edit, 40)
        self._set_fixed_control_height(self._danmaku_source_search_provider_combo, 42)

    def _format_tooltip(self, label: str, shortcut: str | None = None) -> str:
        if shortcut is None:
            return label
        return f"{label} ({shortcut})"

    def _is_always_on_top(self) -> bool:
        return self._always_on_top_enabled

    def _uses_xcb_pseudo_maximize(self) -> bool:
        return QApplication.platformName().strip().lower() == "xcb"

    def _is_effectively_maximized(self) -> bool:
        return bool(getattr(self, "_pseudo_maximized", False)) or self.isMaximized()

    def _normal_geometry_for_title_bar_restore(self) -> QRect:
        if self._pseudo_maximized and self._normal_geometry_before_pseudo_maximize is not None:
            return QRect(self._normal_geometry_before_pseudo_maximize)
        return super()._normal_geometry_for_title_bar_restore()

    def _restore_from_effective_maximized(self) -> None:
        if self._pseudo_maximized:
            self._leave_pseudo_maximized()
            return
        super()._restore_from_effective_maximized()

    def _enter_pseudo_maximized(self, *, normal_geometry: QRect | None = None) -> None:
        if self._pseudo_maximized:
            return
        if normal_geometry is None:
            normal_geometry = self.normalGeometry() if self.isMaximized() else self.geometry()
        normal_geometry = QRect(normal_geometry)
        if self.isMaximized():
            self.showNormal()
        self.setGeometry(normal_geometry)
        self._normal_geometry_before_pseudo_maximize = QRect(normal_geometry)
        self._normal_window_state_before_pseudo_maximize = qbytearray_to_bytes(
            self.saveGeometry()
        )
        self._pseudo_maximized = True
        self.setGeometry(self.screen().availableGeometry())
        self._update_window_chrome_state()

    def _leave_pseudo_maximized(self) -> None:
        if not self._pseudo_maximized:
            return
        normal_geometry = self._normal_geometry_before_pseudo_maximize
        self._pseudo_maximized = False
        if normal_geometry is not None:
            self.setGeometry(normal_geometry)
        self._update_window_chrome_state()

    def _toggle_maximized(self) -> None:
        if not self._uses_xcb_pseudo_maximize():
            super()._toggle_maximized()
            return
        if self._pseudo_maximized:
            self._leave_pseudo_maximized()
        elif self.isMaximized():
            self.showNormal()
        else:
            self._enter_pseudo_maximized()
        self._update_window_chrome_state()

    def _convert_true_maximize_to_pseudo_maximize(self) -> bool:
        if (
            not self._uses_xcb_pseudo_maximize()
            or self._pseudo_maximized
            or not self.isVisible()
            or not self.isMaximized()
            or self.isMinimized()
        ):
            return False
        self._enter_pseudo_maximized(normal_geometry=self.normalGeometry())
        return True

    @classmethod
    def _decode_pseudo_maximized_geometry(
        cls,
        saved_geometry: bytes,
    ) -> tuple[QRect, bytes] | None:
        if not saved_geometry.startswith(cls._PSEUDO_MAXIMIZED_GEOMETRY_PREFIX):
            return None
        payload = saved_geometry[len(cls._PSEUDO_MAXIMIZED_GEOMETRY_PREFIX) :]
        if len(payload) <= cls._PSEUDO_MAXIMIZED_GEOMETRY_RECT_SIZE:
            return None
        x, y, width, height = struct.unpack(
            ">4i",
            payload[: cls._PSEUDO_MAXIMIZED_GEOMETRY_RECT_SIZE],
        )
        if width <= 0 or height <= 0:
            return None
        return (
            QRect(x, y, width, height),
            payload[cls._PSEUDO_MAXIMIZED_GEOMETRY_RECT_SIZE :],
        )

    def _restore_player_window_geometry(self, saved_geometry: bytes) -> None:
        decoded = self._decode_pseudo_maximized_geometry(bytes(saved_geometry))
        if decoded is None:
            self.restoreGeometry(to_qbytearray(saved_geometry))
            return
        normal_geometry, normal_window_state = decoded
        self.restoreGeometry(to_qbytearray(normal_window_state))
        self.setGeometry(normal_geometry)
        self._restored_pseudo_normal_geometry = QRect(normal_geometry)
        self._restore_pseudo_maximized_on_show = True

    def _set_native_always_on_top(self, enabled: bool) -> None:
        # Qt flag changes can invalidate libmpv's native child after hide/show
        # on X11, so ask the window manager to change stacking in place.
        if QApplication.platformName().strip().lower() == "xcb":
            set_x11_window_above(int(self.winId()), enabled)
            return
        handle = self.windowHandle()
        if handle is None:
            self.winId()
            handle = self.windowHandle()
        if handle is None:
            raise RuntimeError("player native window is unavailable")
        # Avoid QWidget.setWindowFlag(), which explicitly hides the window.
        handle.setFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)

    def _should_apply_always_on_top(self) -> bool:
        return self._always_on_top_enabled and self.is_playing

    def _sync_native_always_on_top(self, *, failure_message: str) -> bool:
        desired = self._should_apply_always_on_top()
        converted_from_true_maximize = (
            self._convert_true_maximize_to_pseudo_maximize() if desired else False
        )
        if desired == self._always_on_top_applied and not converted_from_true_maximize:
            return True
        try:
            self._set_native_always_on_top(desired)
        except Exception as exc:
            if converted_from_true_maximize:
                self._leave_pseudo_maximized()
                self.showMaximized()
            logger.exception("PlayerWindow playback always-on-top synchronization failed")
            try:
                self._append_log(f"{failure_message}: {exc}")
            except Exception:
                pass
            return False
        self._always_on_top_applied = desired
        if desired:
            if self.isVisible() and not self.isMinimized():
                self.raise_()
        return True

    def _sync_always_on_top_controls(self, *, menu_action: QAction | None = None) -> bool:
        enabled = self._is_always_on_top()
        action = menu_action or self._always_on_top_menu_action
        button = getattr(self, "always_on_top_button", None)
        label = "取消播放时置顶" if enabled else "播放时置顶"
        if button is not None:
            previous_block_state = button.blockSignals(True)
            try:
                button.setChecked(enabled)
                button.setToolTip(label)
                button.setAccessibleName(label)
                icon_name = "pin-filled.svg" if enabled else "pin.svg"
                button.setProperty("icon_name", icon_name)
                button.setIcon(load_icon(self._icons_dir / icon_name))
            finally:
                button.blockSignals(previous_block_state)
        if action is not None:
            previous_block_state = action.blockSignals(True)
            try:
                action.setChecked(enabled)
            finally:
                action.blockSignals(previous_block_state)
        return enabled

    def _set_always_on_top(
        self,
        enabled: bool,
        *,
        menu_action: QAction | None = None,
    ) -> None:
        requested = bool(enabled)
        previous = self._always_on_top_enabled
        if requested != previous:
            self._always_on_top_enabled = requested
            if not self._sync_native_always_on_top(failure_message="置顶切换失败"):
                self._always_on_top_enabled = previous
        self._sync_always_on_top_controls(menu_action=menu_action)

    def _apply_favorite_button_theme(self) -> None:
        tokens = current_theme_manager().tokens_for(current_resolved_theme())
        qss = (
            f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 10px;
                padding: 0;
            }}
            QPushButton:hover {{
                background-color: {tokens.panel_alt_bg};
            }}
            QPushButton:pressed {{
                background-color: {tokens.input_bg};
            }}
            """
        )
        self.favorite_button.setStyleSheet(qss)
        self.following_button.setStyleSheet(qss)
        self._refresh_favorite_button()
        self._refresh_following_button()

    def _create_icon_button(
        self,
        icon_name: str,
        tooltip: str,
        shortcut: str | None = None,
        *,
        role: str = "secondary",
    ) -> QPushButton:
        button = QPushButton("")
        button.setToolTip(self._format_tooltip(tooltip, shortcut))
        button.setProperty("control_role", role)
        button.setProperty("icon_name", icon_name)
        button.setProperty("use_tinted_icon", icon_name in self._TINTED_ICON_NAMES)
        button.setIcon(load_icon(self._icons_dir / icon_name))
        if role == "primary":
            button.setFixedSize(42, 42)
            button.setIconSize(QSize(22, 22))
        else:
            button.setFixedSize(34, 34)
            button.setIconSize(QSize(18, 18))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _create_skip_spinbox(self, prefix: str) -> QSpinBox:
        spinbox = QSpinBox()
        spinbox.setPrefix(prefix)
        spinbox.setSuffix("s")
        spinbox.setRange(0, 240)
        spinbox.setFixedHeight(28)
        spinbox.setFixedWidth(105)
        spinbox.setSingleStep(10)
        return spinbox

    def _configure_control_combo(
        self,
        combo: QComboBox,
        *,
        minimum_contents_length: int,
        maximum_width: int | None = None,
        fixed_height: int | None = None,
    ) -> None:
        combo.setCursor(Qt.CursorShape.PointingHandCursor)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(minimum_contents_length)
        combo.setMaxVisibleItems(12)
        if fixed_height is not None:
            size_policy = combo.sizePolicy()
            combo.setSizePolicy(size_policy.horizontalPolicy(), QSizePolicy.Policy.Fixed)
            combo.setFixedHeight(fixed_height)
        if maximum_width is not None:
            combo.setMaximumWidth(maximum_width)

    def _update_play_button_icon(self) -> None:
        icon_name = "pause.svg" if self.is_playing else "play.svg"
        self.play_button.setIcon(load_icon(self._icons_dir / icon_name))

    def _resolve_pending_pause(
        self, pending_pause: bool, intent_generation: int
    ) -> bool:
        # 延迟加载完成时,发起时刻捕获的 pause 可能已过期(用户在解析期间
        # 按了暂停/播放)。出现过 toggle 就以最新意图为准,否则沿用捕获值
        # (如"下一集"在暂停状态下仍自动播放)。
        if intent_generation == self._playback_intent_generation:
            return pending_pause
        return not self.is_playing

    def _handle_pause_state_changed(self, paused: bool) -> None:
        # mpv 的 pause 属性是播放状态的事实来源;按钮此前只反映乐观更新的
        # is_playing,任何一侧自行变化(延迟加载、失败、脚本)都会漂移,
        # 这里用事件把 UI 拉回真实状态。
        if self.session is None:
            return
        playing = not paused
        if playing == self.is_playing:
            return
        self.is_playing = playing
        self._set_last_player_paused(paused)
        self._update_play_button_icon()

    def _mark_playback_stopped(self) -> None:
        if not self.is_playing:
            return
        self.is_playing = False
        self._set_last_player_paused(True)
        self._update_play_button_icon()

    def _default_window_title(self) -> str:
        return "alist-tvbox 播放器"

    def _source_label(self) -> str:
        # Resolved by MainWindow._prepare_request_for_open (honors tab/plugin renames).
        return str(getattr(self.session, "source_display_name", "") or "").strip()

    def _detail_field_value(self, fields: list[PlaybackDetailField], label: str) -> str:
        for field in fields:
            if str(field.label or "").strip() == label:
                value = str(field.value or "").strip()
                if value:
                    return value
        return ""

    def _is_placeholder_playback_title(self, title: str) -> bool:
        normalized = str(title or "").strip()
        if not normalized:
            return True
        lowered = normalized.lower()
        return looks_like_youtube_video_id(normalized) or lowered.startswith(("http://", "https://", "yt:video:"))

    def _active_media_title(self, current_item: PlayItem) -> str:
        if self.session is None:
            return ""
        vod_title = str(self.session.vod.vod_name or "").strip()
        item_title = playlist_item_display_title(current_item, "episode").strip()
        channel_title = self._detail_field_value(self.session.vod.detail_fields, "频道") or self._detail_field_value(
            current_item.detail_fields,
            "频道",
        )
        if channel_title and vod_title == item_title:
            return channel_title
        initial_title = str(getattr(self.session, "initial_vod_name", "") or "").strip()
        if (
            initial_title
            and initial_title != vod_title
            and initial_title != item_title
            and not self._is_placeholder_playback_title(initial_title)
        ):
            return initial_title
        return vod_title

    def _active_playback_title(self) -> str:
        if self.session is None or not self.session.playlist:
            return self._default_window_title()
        current_item = self.session.playlist[self.current_index]
        episode_title = playlist_item_display_title(current_item, "episode").strip()
        if getattr(self.session, "drive_resource_id", ""):
            # Drive share collection: 合集标题 - 子目录(剧名) - 文件名
            # Use the group label (directory name) directly — current_item.media_title gets
            # overwritten back to vod_name by metadata hydration sync.
            vod_title = str(self.session.vod.vod_name or "").strip()
            groups = self._session_source_groups()
            gi = self.session.source_group_index
            dir_title = ""
            if 0 <= gi < len(groups) and groups[gi].sources:
                source = groups[gi].sources[self.session.source_index]
                if source.subgroups and 0 <= source.subgroup_index < len(source.subgroups):
                    dir_title = clean_drive_directory_title(source.subgroups[source.subgroup_index].label)
                else:
                    dir_title = clean_drive_directory_title(groups[gi].label)
            # Only use the three-part form when the directory differs from the collection title
            # (i.e. a real multi-folder share); otherwise it's a single resource — no repeat.
            if dir_title and dir_title != vod_title:
                parts = [p for p in [vod_title, dir_title, episode_title] if p]
            else:
                parts = [p for p in [vod_title, episode_title] if p]
        else:
            parts = [p for p in [self._active_media_title(current_item), episode_title] if p]
        source_label = self._source_label()
        if source_label:
            parts.append(source_label)
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

    def _build_source_groups_from_playlists(self, playlists: list[list[PlayItem]]) -> list[PlaybackSourceGroup]:
        source_groups: list[PlaybackSourceGroup] = []
        for index, playlist in enumerate(playlists):
            label = self._playlist_group_label(playlist, index)
            source_groups.append(
                PlaybackSourceGroup(
                    label=label,
                    sources=[PlaybackSource(label=label, playlist=playlist)],
                )
            )
        return source_groups

    def _flatten_source_groups(
        self,
        source_groups: list[PlaybackSourceGroup],
    ) -> tuple[list[list[PlayItem]], dict[tuple[int, int], int]]:
        playlists: list[list[PlayItem]] = []
        mapping: dict[tuple[int, int], int] = {}
        for group_index, group in enumerate(source_groups):
            for source_index, source in enumerate(group.sources):
                mapping[(group_index, source_index)] = len(playlists)
                playlists.append(source.playlist)
        return playlists, mapping

    def _session_source_groups(self) -> list[PlaybackSourceGroup]:
        if self.session is None:
            return []
        if self.session.source_groups:
            return self.session.source_groups
        return self._build_source_groups_from_playlists(self._session_playlists())

    def _playlist_group_label(self, playlist: list[PlayItem], playlist_index: int) -> str:
        item_count = len(playlist)
        if playlist and playlist[0].play_source:
            label = playlist[0].play_source
            return f"{label}({item_count})" if item_count > 1 else label
        return f"线路 {playlist_index + 1}"

    def _render_playlist_source_combos(self) -> None:
        source_groups = self._session_source_groups()
        self.playlist_group_combo.blockSignals(True)
        self.playlist_source_combo.blockSignals(True)
        self.playlist_subgroup_combo.blockSignals(True)
        self.playlist_group_combo.clear()
        self.playlist_source_combo.clear()
        self.playlist_subgroup_combo.clear()
        for group in source_groups:
            self.playlist_group_combo.addItem(group.label)
        active_group: PlaybackSourceGroup | None = None
        if self.session is not None and source_groups:
            self.session.source_group_index = max(0, min(self.session.source_group_index, len(source_groups) - 1))
            active_group = source_groups[self.session.source_group_index]
            self.session.source_index = max(0, min(self.session.source_index, len(active_group.sources) - 1))
            for source in active_group.sources:
                self.playlist_source_combo.addItem(source.label)
            self.playlist_group_combo.setCurrentIndex(self.session.source_group_index)
            self.playlist_source_combo.setCurrentIndex(self.session.source_index)
            active_source = active_group.sources[self.session.source_index]
            subgroups = active_source.subgroups
            active_source.subgroup_index = max(0, min(active_source.subgroup_index, len(subgroups) - 1)) if subgroups else 0
            for subgroup in subgroups:
                self.playlist_subgroup_combo.addItem(subgroup.label)
            if subgroups:
                self.playlist_subgroup_combo.setCurrentIndex(active_source.subgroup_index)
        self.playlist_group_combo.setHidden(len(source_groups) <= 1)
        self.playlist_source_combo.setHidden(active_group is None or len(active_group.sources) <= 1)
        active_source = active_group.sources[self.session.source_index] if active_group is not None and active_group.sources else None
        self.playlist_subgroup_combo.setHidden(active_source is None or len(active_source.subgroups) <= 1)
        self.playlist_group_combo.blockSignals(False)
        self.playlist_source_combo.blockSignals(False)
        self.playlist_subgroup_combo.blockSignals(False)

    def _change_playlist_title_mode(self, index: int) -> None:
        self.playlist_title_mode = "original" if index == 1 else "episode"
        self._render_playlist_items()
        self._render_bilibili_playlist_tree()

    def _supports_bilibili_grouped_playlist_tree(self) -> bool:
        return bool(
            self.session is not None
            and str(getattr(self.session, "source_kind", "") or "").strip() == "bilibili"
            and len(self.session.playlists) > 1
        )

    def _effective_playlist_panel_mode(self) -> str:
        if self._playlist_panel_mode == "hidden":
            return self._playlist_panel_resume_mode
        return self._playlist_panel_mode

    def _allowed_playlist_panel_modes(self) -> tuple[str, ...]:
        if self._supports_bilibili_grouped_playlist_tree():
            return ("list", "tree", "hidden")
        return ("list", "hidden")

    def _normalize_playlist_panel_mode(self, mode: str) -> str:
        allowed_modes = self._allowed_playlist_panel_modes()
        if mode in allowed_modes:
            return mode
        return "list"

    def _playlist_panel_mode_tooltip(self, mode: str) -> str:
        labels = {
            "list": "播放列表：普通列表",
            "tree": "播放列表：分组树",
            "hidden": "播放列表：隐藏",
        }
        return labels.get(mode, labels["list"])

    def _update_playlist_toggle_button_state(self) -> None:
        mode = self._normalize_playlist_panel_mode(self._playlist_panel_mode)
        self._playlist_panel_mode = mode
        icon_name = "grid.svg" if mode == "tree" else "queue.svg"
        self.toggle_playlist_button.setChecked(mode != "hidden")
        self._set_button_icon(self.toggle_playlist_button, icon_name)
        self.toggle_playlist_button.setToolTip(self._playlist_panel_mode_tooltip(mode))

    def _playlist_panel_visible(self) -> bool:
        return (
            not self.isFullScreen()
            and not self.wide_button.isChecked()
            and self._playlist_panel_mode != "hidden"
        )

    def _bilibili_grouped_playlist_tree_enabled(self) -> bool:
        return bool(
            self._supports_bilibili_grouped_playlist_tree()
            and self._effective_playlist_panel_mode() == "tree"
        )

    def _restore_bilibili_standard_playlist(self) -> None:
        if self.session is None or not self._supports_bilibili_grouped_playlist_tree():
            return
        current_item = self.session.playlist[self.current_index] if 0 <= self.current_index < len(self.session.playlist) else None
        group_index = self.session.playlist_index
        item_index = 0
        if current_item is not None:
            for candidate_group_index, playlist in enumerate(self.session.playlists):
                try:
                    item_index = playlist.index(current_item)
                except ValueError:
                    continue
                group_index = candidate_group_index
                break
        group_index = max(0, min(group_index, len(self.session.playlists) - 1))
        target_playlist = self.session.playlists[group_index]
        self.session.playlist_index = group_index
        self.session.source_group_index = group_index
        self.session.source_index = 0
        self.session.playlist = target_playlist
        self.current_index = max(0, min(item_index, len(target_playlist) - 1)) if target_playlist else 0
        self.session.start_index = self.current_index
        self._bilibili_tree_flat_index_by_item_id = {}
        self._bilibili_tree_flat_index_by_group_item = {}
        self._bilibili_tree_group_item_by_flat_index = {}

    def _set_playlist_panel_mode(self, mode: str, *, persist_default: bool = False) -> None:
        normalized_mode = self._normalize_playlist_panel_mode(mode)
        previous_effective_mode = self._effective_playlist_panel_mode()
        resume_mode = self._playlist_panel_resume_mode
        if normalized_mode in {"list", "tree"}:
            resume_mode = normalized_mode
        next_effective_mode = resume_mode if normalized_mode == "hidden" else normalized_mode
        if self._supports_bilibili_grouped_playlist_tree() and previous_effective_mode != next_effective_mode:
            if next_effective_mode == "tree":
                self._activate_bilibili_tree_playlist(
                    preferred_group_item=(self.session.playlist_index, self.current_index) if self.session is not None else None
                )
            else:
                self._restore_bilibili_standard_playlist()
        self._playlist_panel_mode = normalized_mode
        self._playlist_panel_resume_mode = resume_mode
        if (
            persist_default
            and normalized_mode in {"list", "tree"}
            and self.config is not None
            and self._supports_bilibili_grouped_playlist_tree()
        ):
            enabled = normalized_mode == "tree"
            if getattr(self.config, "bilibili_grouped_playlist_tree_enabled", False) != enabled:
                self.config.bilibili_grouped_playlist_tree_enabled = enabled
                self._save_config()
        if previous_effective_mode != next_effective_mode:
            self._render_playlist_source_combos()
            self._render_playlist_items()
            self._render_bilibili_playlist_tree()
        self._update_playlist_toggle_button_state()
        self._apply_visibility_state()

    def _cycle_playlist_panel_mode(self) -> None:
        allowed_modes = self._allowed_playlist_panel_modes()
        try:
            current_index = allowed_modes.index(self._playlist_panel_mode)
        except ValueError:
            current_index = 0
        next_mode = allowed_modes[(current_index + 1) % len(allowed_modes)]
        self._set_playlist_panel_mode(next_mode, persist_default=True)

    def _sync_playlist_panel_mode(self) -> None:
        self._playlist_panel_mode = self._normalize_playlist_panel_mode(self._playlist_panel_mode)
        self._update_playlist_toggle_button_state()
        self._apply_visibility_state()

    def _build_bilibili_tree_flat_playlist(
        self,
    ) -> tuple[list[PlayItem], dict[int, int], dict[tuple[int, int], int], dict[int, tuple[int, int]]]:
        if self.session is None:
            return [], {}, {}, {}
        flat_playlist: list[PlayItem] = []
        flat_index_by_item_id: dict[int, int] = {}
        flat_index_by_group_item: dict[tuple[int, int], int] = {}
        group_item_by_flat_index: dict[int, tuple[int, int]] = {}
        for group_index, playlist in enumerate(self.session.playlists):
            for item_index, play_item in enumerate(playlist):
                flat_index = len(flat_playlist)
                flat_playlist.append(play_item)
                flat_index_by_item_id[id(play_item)] = flat_index
                flat_index_by_group_item[(group_index, item_index)] = flat_index
                group_item_by_flat_index[flat_index] = (group_index, item_index)
        return flat_playlist, flat_index_by_item_id, flat_index_by_group_item, group_item_by_flat_index

    def _sync_bilibili_tree_active_group_from_current_index(self) -> None:
        if self.session is None or not self._supports_bilibili_grouped_playlist_tree():
            return
        group_item = self._bilibili_tree_group_item_by_flat_index.get(self.current_index)
        if group_item is None:
            return
        group_index, _item_index = group_item
        self.session.playlist_index = group_index
        self.session.source_group_index = group_index
        self.session.source_index = 0

    def _activate_bilibili_tree_playlist(
        self,
        *,
        preferred_group_item: tuple[int, int] | None = None,
    ) -> None:
        if self.session is None or not self._supports_bilibili_grouped_playlist_tree():
            return
        current_item = self.session.playlist[self.current_index] if 0 <= self.current_index < len(self.session.playlist) else None
        (
            flat_playlist,
            self._bilibili_tree_flat_index_by_item_id,
            self._bilibili_tree_flat_index_by_group_item,
            self._bilibili_tree_group_item_by_flat_index,
        ) = self._build_bilibili_tree_flat_playlist()
        if not flat_playlist:
            return
        self.session.playlist = flat_playlist
        if preferred_group_item is not None:
            self.current_index = self._bilibili_tree_flat_index_by_group_item.get(preferred_group_item, 0)
        elif current_item is not None:
            self.current_index = self._bilibili_tree_flat_index_by_item_id.get(id(current_item), 0)
        else:
            self.current_index = 0
        self.session.start_index = self.current_index
        self._sync_bilibili_tree_active_group_from_current_index()

    def _render_playlist_title_tabs(self) -> None:
        playlist = list(self.session.playlist if self.session is not None else [])
        visible = self._playlist_panel_visible() and not self._bilibili_grouped_playlist_tree_enabled() and playlist_has_title_variants(playlist)
        self.playlist_title_tabs.setHidden(not visible)
        self.playlist_title_tabs.blockSignals(True)
        self.playlist_title_tabs.setCurrentIndex(0 if self.playlist_title_mode == "episode" else 1)
        self.playlist_title_tabs.blockSignals(False)

    def _render_playlist_sort_combo(self) -> None:
        playlist = self.session.playlist if self.session is not None else []
        options = self._playlist_sort_state.options_for(playlist)
        supported = {option.value for option in options}
        if self._playlist_sort_state.mode not in supported:
            self._playlist_sort_state.mode = ORIGINAL
        self.playlist_sort_combo.blockSignals(True)
        self.playlist_sort_combo.clear()
        for option in options:
            self.playlist_sort_combo.addItem(option.label, option.value)
        selected = self.playlist_sort_combo.findData(self._playlist_sort_state.mode)
        self.playlist_sort_combo.setCurrentIndex(max(0, selected))
        self.playlist_sort_combo.blockSignals(False)
        visible = (
            self._playlist_panel_visible()
            and not self._bilibili_grouped_playlist_tree_enabled()
            and len(options) > 1
        )
        self.playlist_sort_combo.setHidden(not visible)

    def _apply_playlist_sort(self, current_item: PlayItem | None = None) -> None:
        if self.session is None:
            return
        fallback = self.current_index
        if current_item is None and 0 <= fallback < len(self.session.playlist):
            current_item = self.session.playlist[fallback]
        self._playlist_sort_state.apply(self.session.playlist)
        self.current_index = find_playlist_item_index(
            self.session.playlist,
            current_item,
            fallback,
        )
        self.session.start_index = self.current_index
        self._render_playlist_sort_combo()
        self._render_playlist_items()

    def _change_playlist_sort(self, _index: int) -> None:
        if self.session is None:
            return
        mode = str(self.playlist_sort_combo.currentData() or ORIGINAL)
        current_item = (
            self.session.playlist[self.current_index]
            if 0 <= self.current_index < len(self.session.playlist)
            else None
        )
        self._playlist_sort_state.mode = mode
        self._apply_playlist_sort(current_item)

    def _render_playlist_items(self) -> None:
        self.playlist.clear()
        if self.session is None:
            return
        for item in self.session.playlist:
            display_title = playlist_item_display_title(item, self.playlist_title_mode)
            widget_item = QListWidgetItem(self._playlist_item_display_text(item, display_title))
            widget_item.setToolTip(self._playlist_item_tooltip(item, display_title))
            self.playlist.addItem(widget_item)
        self.playlist.setCurrentRow(self.current_index)
        self._sync_playlist_item_styles()

    @staticmethod
    def _playlist_item_display_text(item: PlayItem, display_title: str) -> str:
        """Keep drive/file sizes visible after episode-title rewriting."""
        size = int(getattr(item, "size", 0) or 0)
        if size <= 0:
            for candidate in (getattr(item, "original_title", ""), getattr(item, "title", "")):
                size = parse_size_bytes(candidate)
                if size > 0:
                    break
        if size <= 0 or re.search(r"\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB)\b", display_title, re.IGNORECASE):
            return display_title
        return f"{display_title} ({format_size_bytes(size)})"

    def _playlist_item_tooltip(self, item: PlayItem, display_title: str) -> str:
        tooltip = display_title
        original_title = str(item.original_title or "").strip()
        episode_title = str(item.episode_display_title or "").strip()
        if (
            self.playlist_title_mode == "episode"
            and original_title
            and episode_title
            and normalize_episode_title_text(original_title) != normalize_episode_title_text(episode_title)
        ):
            tooltip = original_title
        return tooltip

    def _render_bilibili_playlist_tree(self) -> None:
        self.bilibili_playlist_tree.clear()
        if self.session is None or not self._supports_bilibili_grouped_playlist_tree():
            return
        for group_index, playlist in enumerate(self.session.playlists):
            if not playlist:
                continue
            group_item = QTreeWidgetItem([self._playlist_group_label(playlist, group_index)])
            group_item.setData(0, Qt.ItemDataRole.UserRole, ("group", group_index, -1))
            group_item.setExpanded(True)
            self.bilibili_playlist_tree.addTopLevelItem(group_item)
            for item_index, play_item in enumerate(playlist):
                display_title = playlist_item_display_title(play_item, self.playlist_title_mode)
                leaf = QTreeWidgetItem([self._playlist_item_display_text(play_item, display_title)])
                leaf.setData(0, Qt.ItemDataRole.UserRole, ("leaf", group_index, item_index))
                leaf.setToolTip(0, self._playlist_item_tooltip(play_item, display_title))
                group_item.addChild(leaf)
        self._sync_bilibili_tree_item_styles()

    def _sync_bilibili_tree_item_styles(self) -> None:
        if self.session is None or not self._supports_bilibili_grouped_playlist_tree():
            return
        tokens = current_theme_manager().tokens_for(current_resolved_theme())
        for flat_index, group_item_key in self._bilibili_tree_group_item_by_flat_index.items():
            group_index, item_index = group_item_key
            group_item = self.bilibili_playlist_tree.topLevelItem(group_index)
            if group_item is None or item_index >= group_item.childCount():
                continue
            leaf = group_item.child(item_index)
            font = leaf.font(0)
            if flat_index == self.current_index:
                leaf.setForeground(0, QBrush(QColor(tokens.accent)))
                font.setBold(True)
            elif flat_index < self.current_index:
                leaf.setForeground(0, QBrush(QColor(tokens.text_secondary)))
                font.setBold(False)
            else:
                leaf.setForeground(0, QBrush(QColor(tokens.text_primary)))
                font.setBold(False)
            leaf.setFont(0, font)
        current_group_item = self._bilibili_tree_group_item_by_flat_index.get(self.current_index)
        if current_group_item is not None:
            group_index, item_index = current_group_item
            group_item = self.bilibili_playlist_tree.topLevelItem(group_index)
            if group_item is not None and item_index < group_item.childCount():
                group_item.setExpanded(True)
                self.bilibili_playlist_tree.setCurrentItem(group_item.child(item_index))

    def _handle_bilibili_tree_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        payload = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(payload, tuple) or not payload or payload[0] != "leaf":
            item.setExpanded(not item.isExpanded())
            return

    def _handle_bilibili_tree_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        payload = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(payload, tuple) or not payload or payload[0] != "leaf":
            item.setExpanded(not item.isExpanded())
            return
        if self.session is None:
            return
        flat_index = self._bilibili_tree_flat_index_by_group_item.get((payload[1], payload[2]))
        if flat_index is None or flat_index == self.current_index:
            return
        self.report_progress(force_remote_report=True)
        self._stop_current_playback()
        self._play_item_at_index(flat_index, preserve_primary_external_subtitle_selection=True)

    def _sync_playlist_item_styles(self) -> None:
        if self.session is None:
            return
        tokens = current_theme_manager().tokens_for(current_resolved_theme())
        current_icon = self._build_playlist_state_icon(tokens.accent)
        for index in range(self.playlist.count()):
            playlist_item = self.playlist.item(index)
            font = playlist_item.font()
            if index == self.current_index:
                playlist_item.setForeground(QBrush(QColor(tokens.accent)))
                font.setBold(True)
                playlist_item.setIcon(current_icon)
            elif index < self.current_index:
                playlist_item.setForeground(QBrush(QColor(tokens.text_secondary)))
                font.setBold(False)
                playlist_item.setIcon(QIcon())
            else:
                playlist_item.setForeground(QBrush(QColor(tokens.text_primary)))
                font.setBold(False)
                playlist_item.setIcon(QIcon())
            playlist_item.setFont(font)

    def _build_playlist_state_icon(self, color: str) -> QIcon:
        pixmap = QPixmap(10, 10)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(1, 1, 8, 8)
        painter.end()
        return QIcon(pixmap)

    def _current_detail_actions(self) -> list[PlaybackDetailAction]:
        if self.session is None or not (0 <= self.current_index < len(self.session.playlist)):
            return []
        return [action for action in self.session.playlist[self.current_index].detail_actions if action.visible]

    def _clone_metadata_snapshot(self, vod: VodItem) -> VodItem:
        return deepcopy(vod)

    def _current_metadata_vod(self) -> VodItem | None:
        if self.session is None:
            return None
        if self.session.show_original_metadata and self.session.original_vod is not None:
            return self.session.original_vod
        return self.session.vod

    def _metadata_values_differ(self) -> bool:
        if self.session is None or self.session.original_vod is None:
            return False
        original = self.session.original_vod
        current = self.session.vod
        return (
            original.vod_name != current.vod_name
            or original.type_name != current.type_name
            or original.vod_year != current.vod_year
            or original.vod_area != current.vod_area
            or original.vod_lang != current.vod_lang
            or original.vod_remarks != current.vod_remarks
            or original.vod_director != current.vod_director
            or original.vod_actor != current.vod_actor
            or original.vod_content != current.vod_content
            or original.dbid != current.dbid
            or original.detail_fields != current.detail_fields
        )

    def _refresh_metadata_original_toggle(self) -> None:
        is_youtube_detail = bool(
            self.session is not None
            and (
                getattr(self.session.vod, "detail_style", "") == "youtube"
                or (
                    self.session.original_vod is not None
                    and getattr(self.session.original_vod, "detail_style", "") == "youtube"
                )
            )
        )
        visible = (
            self.session is not None
            and not is_youtube_detail
            and not self.details.isHidden()
            and self._metadata_values_differ()
        )
        if self.session is not None and not visible:
            self.session.show_original_metadata = False
        self._metadata_original_toggle.blockSignals(True)
        self._metadata_original_toggle.setChecked(bool(self.session and self.session.show_original_metadata))
        self._metadata_original_toggle.setToolTip(
            "显示增强后详情" if self.session and self.session.show_original_metadata else "显示原始详情"
        )
        self._metadata_original_toggle.blockSignals(False)
        self._metadata_original_toggle.setHidden(not visible)

    def _toggle_original_metadata_view(self, checked: bool) -> None:
        if self.session is None:
            return
        self.session.show_original_metadata = checked
        self._reset_metadata_poster_index()
        self._refresh_metadata_original_toggle()
        self._render_poster()
        self._render_metadata()
        self._render_detail_fields()

    def _snapshot_item_detail_fields(self, item: PlayItem) -> None:
        if self.session is None:
            return
        key = self._playlist_identity_key(item)
        if key not in self.session.original_item_detail_fields_by_key:
            self.session.original_item_detail_fields_by_key[key] = deepcopy(item.detail_fields)

    def _build_original_metadata_snapshot(self, vod: VodItem) -> VodItem:
        snapshot = self._clone_metadata_snapshot(vod)
        if self.session is None:
            return snapshot
        current_item = self._current_play_item()
        if current_item is None:
            return snapshot
        key = self._playlist_identity_key(current_item)
        cached_fields = self.session.original_item_detail_fields_by_key.get(key)
        if cached_fields:
            snapshot.detail_fields = deepcopy(cached_fields)
        return snapshot

    def _update_original_metadata_snapshot(self, vod: VodItem) -> None:
        if self.session is None:
            return
        self.session.original_vod = self._build_original_metadata_snapshot(vod)
        self._refresh_metadata_original_toggle()

    def _current_detail_fields(self) -> list[PlaybackDetailField]:
        if self.session is None:
            return []
        if self.session.show_original_metadata and self.session.original_vod is not None:
            return self._visible_metadata_detail_fields(self.session.original_vod.detail_fields)
        if self._is_bilibili_metadata_session():
            item_fields = []
            if 0 <= self.current_index < len(self.session.playlist):
                item_fields = self.session.playlist[self.current_index].detail_fields
            return self._visible_metadata_detail_fields(self._merge_bilibili_collection_detail_fields(item_fields))
        if 0 <= self.current_index < len(self.session.playlist):
            item_fields = self.session.playlist[self.current_index].detail_fields
            if item_fields:
                return self._visible_metadata_detail_fields(item_fields)
        return self._visible_metadata_detail_fields(self.session.vod.detail_fields)

    def _visible_metadata_detail_fields(self, fields: list[PlaybackDetailField]) -> list[PlaybackDetailField]:
        return [
            field
            for field in fields
            if field.label.strip().lower() not in _HIDDEN_METADATA_DETAIL_LABELS
        ]

    def _is_bilibili_metadata_session(self) -> bool:
        if self.session is None:
            return False
        return (
            str(getattr(self.session, "source_kind", "") or "").strip() == "bilibili"
            or getattr(self.session.vod, "detail_style", "") == "bilibili"
            or (
                self.session.original_vod is not None
                and getattr(self.session.original_vod, "detail_style", "") == "bilibili"
            )
        )

    def _bilibili_identity_detail_fields(self) -> list[PlaybackDetailField]:
        if self.session is None:
            return []
        fields: list[PlaybackDetailField] = []
        seen_labels: set[str] = set()
        for vod in (self.session.vod, self.session.original_vod):
            if vod is None:
                continue
            for field in vod.detail_fields:
                label = field.label.strip().lower()
                if label not in _BILIBILI_IDENTITY_DETAIL_LABELS or label in seen_labels:
                    continue
                fields.append(field)
                seen_labels.add(label)
        return fields

    def _merge_bilibili_identity_detail_fields(
        self, item_fields: list[PlaybackDetailField]
    ) -> list[PlaybackDetailField]:
        if self.session is None:
            return list(item_fields)
        item_labels = {field.label.strip().lower() for field in item_fields}
        identity_fields = [
            field
            for field in self._bilibili_identity_detail_fields()
            if field.label.strip().lower() not in item_labels
        ]
        return [*identity_fields, *item_fields]

    def _merge_bilibili_collection_detail_fields(
        self, item_fields: list[PlaybackDetailField]
    ) -> list[PlaybackDetailField]:
        if self.session is None:
            return list(item_fields)
        fields: list[PlaybackDetailField] = []
        seen_labels: set[str] = set()
        for group in (self._bilibili_identity_detail_fields(), self.session.vod.detail_fields, item_fields):
            for field in group:
                label = field.label.strip().lower()
                if not label or label in seen_labels:
                    continue
                fields.append(field)
                seen_labels.add(label)
        return fields

    def _reset_metadata_poster_index(self) -> None:
        if self.session is None:
            return
        self.session.current_metadata_poster_index = 0
        self._refresh_poster_navigation()

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
        if not self._heat_summary_text:
            self.detail_fields_widget.setHidden(True)
            return
        row = QWidget(self.detail_fields_widget)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        label = QLabel("热度", row)
        value = QLabel(self._heat_summary_text, row)
        value.setWordWrap(True)
        row_layout.addWidget(label)
        row_layout.addWidget(value, 1)
        self.detail_fields_layout.addWidget(row)
        self.detail_fields_widget.setHidden(False)

    def _current_heat_identity(self):
        if self.session is None:
            return None
        return heat_identity_from_vod(self.session.vod, self._current_play_item())

    def _refresh_heat_summary_for_current_item(self) -> None:
        self._heat_summary_text = ""
        self._render_detail_fields()
        heat_controller = self._heat_controller
        media = self._current_heat_identity()
        if heat_controller is None or media is None or not hasattr(heat_controller, "load_media_heat"):
            return
        self._heat_summary_request_id += 1
        request_id = self._heat_summary_request_id
        media_key = media.media_key

        def run() -> None:
            try:
                summary = heat_controller.load_media_heat(media_key)
                text = summary.best_display_text() if summary is not None else ""
            except Exception:
                text = ""
            if self._is_window_alive():
                self._heat_summary_signals.loaded.emit(request_id, text)

        threading.Thread(target=run, daemon=True).start()

    def _handle_heat_summary_loaded(self, request_id: int, text: object) -> None:
        if request_id != self._heat_summary_request_id:
            return
        self._heat_summary_text = str(text or "").strip()
        self._render_detail_fields()

    def _record_heat_effective_watch_if_needed(
        self,
        item: PlayItem | None,
        *,
        position_seconds: int,
        duration_seconds: int,
    ) -> None:
        heat_controller = self._heat_controller
        if item is None or heat_controller is None or not hasattr(heat_controller, "maybe_record_effective_watch"):
            return
        media = self._current_heat_identity()
        if not has_required_heat_external_id(media):
            return
        try:
            heat_controller.maybe_record_effective_watch(
                media,
                position_seconds=int(position_seconds or 0),
                duration_seconds=int(duration_seconds or 0),
                episode_index=int(self.current_index or 0),
                episode_number=self._current_heat_episode_number(item, media),
            )
        except Exception:
            pass

    def _current_heat_episode_number(self, item: PlayItem, media) -> int:
        if str(getattr(media, "media_type", "") or "").strip() == "movie":
            return 0
        playlist = list(getattr(self.session, "playlist", []) or []) if self.session is not None else []
        inferred = infer_playlist_episode_number(item, playlist)
        if inferred is not None and inferred > 0:
            return int(inferred)
        return max(0, int(self.current_index or 0) + 1)

    def _render_detail_actions(self) -> None:
        self._clear_detail_action_buttons()
        self._refresh_favorite_button()
        self._refresh_following_button()
        actions = self._current_detail_actions()
        self.detail_actions_widget.setHidden(not actions)
        for action in actions:
            button = QPushButton(action.label)
            button.setToolTip(action.tooltip)
            button.setEnabled(action.enabled)
            button.setCheckable(True)
            button.setChecked(action.active)
            button.setProperty("detail_action_base_enabled", action.enabled)
            button.clicked.connect(lambda _checked=False, action_id=action.id: self._run_detail_action(action_id))
            self.detail_actions_layout.addWidget(button)

    def _refresh_favorite_button(self) -> None:
        item = self._current_play_item()
        active = item is not None and self._favorite_is_active(item)
        tooltip = "取消收藏" if active else "加入收藏"
        self.favorite_button.setHidden(item is None)
        self.favorite_button.setToolTip(tooltip)
        self.favorite_button.setAccessibleName(tooltip)
        self.favorite_button.setProperty("favorite_active", active)
        self._set_favorite_button_icon(active)

    def _refresh_following_button(self) -> None:
        item = self._current_play_item()
        active = item is not None and self._following_is_active(item)
        tooltip = "取消追更" if active else "加入追更"
        self.following_button.setHidden(item is None)
        self.following_button.setToolTip(tooltip)
        self.following_button.setAccessibleName(tooltip)
        self.following_button.setProperty("following_active", active)
        self._set_following_button_icon(active)

    def _set_favorite_button_icon(self, active: bool) -> None:
        tokens = current_theme_manager().tokens_for(current_resolved_theme())
        icon_name = "favorite-filled.svg" if active else "favorite.svg"
        color = self._FAVORITE_ACTIVE_ICON_COLOR if active else tokens.text_secondary
        self.favorite_button.setProperty("icon_name", icon_name)
        self.favorite_button.setIcon(
            tint_icon(
                load_icon(self._icons_dir / icon_name),
                color,
                size=self.favorite_button.iconSize(),
            )
        )

    def _set_following_button_icon(self, active: bool) -> None:
        tokens = current_theme_manager().tokens_for(current_resolved_theme())
        color = self._FOLLOWING_ACTIVE_ICON_COLOR if active else tokens.text_secondary
        self.following_button.setProperty("icon_name", "following.svg")
        self.following_button.setIcon(
            tint_icon(
                load_icon(self._icons_dir / "following.svg"),
                color,
                size=self.following_button.iconSize(),
            )
        )

    def _toggle_current_favorite(self) -> None:
        item = self._current_play_item()
        if item is None:
            return
        self._favorite_toggle(item)
        self._refresh_favorite_button()

    def _toggle_current_following(self) -> None:
        item = self._current_play_item()
        if item is None:
            return
        self._following_toggle(item)
        self._refresh_following_button()

    def _set_startup_state(self, state: PlaybackStartupState) -> None:
        self._startup_state = state
        self._append_startup_state_log(state)
        action_keys = {action.key for action in state.actions}
        self.playback_retry_button.setVisible("retry" in action_keys)
        self.playback_switch_line_button.setVisible("switch_line" in action_keys)
        self.playback_switch_parser_button.setVisible("switch_parser" in action_keys)
        self.playback_startup_widget.setHidden(state.stage is PlaybackStartupStage.IDLE)

    def _append_startup_state_log(self, state: PlaybackStartupState) -> None:
        if state.stage is not PlaybackStartupStage.RESOLVING:
            return
        self._append_log(state.message, dedupe=True)

    def _resolving_startup_message(self, current_item: PlayItem) -> str:
        source_url = self._current_item_source_address(current_item)
        if source_url:
            return f"正在解析视频详情: {source_url}"
        return f"正在解析视频详情： {current_item.vod_id}"

    def _current_item_source_address(self, current_item: PlayItem) -> str:
        for candidate in (
            current_item.original_url,
            current_item.vod_id,
            current_item.path,
            current_item.url,
        ):
            source = str(candidate or "").strip()
            if not source:
                continue
            if source.startswith(("http://", "https://", "magnet:", "ftp://", "file://", "data:")):
                return source
            if source.startswith("/"):
                return source
        return ""

    def _has_multiple_playback_sources(self) -> bool:
        if self.session is None:
            return False
        return sum(len(group.sources) for group in self._session_source_groups()) > 1

    def _show_failed_startup_state(self, message: str) -> None:
        self._set_startup_state(
            self._startup_coordinator.failed(
                message=message,
                parse_required=self._current_item_requires_parse(),
                has_multiple_sources=self._has_multiple_playback_sources(),
            )
        )

    def _reset_auto_switched_failure_sources(self) -> None:
        self._auto_switched_failure_sources.clear()

    def _current_source_attempt_key(self) -> tuple[int, int] | None:
        if self.session is None:
            return None
        return (self.session.source_group_index, self.session.source_index)

    def _next_source_after_failure(self) -> tuple[int, int] | None:
        if self.session is None:
            return None
        source_groups = self._session_source_groups()
        if not source_groups:
            return None
        active_group = source_groups[self.session.source_group_index]
        if self.session.source_index + 1 < len(active_group.sources):
            return (self.session.source_group_index, self.session.source_index + 1)
        if self.session.source_group_index + 1 < len(source_groups):
            return (self.session.source_group_index + 1, 0)
        return None

    def _should_auto_switch_source_after_failure(self) -> bool:
        return bool(getattr(self.config, "playback_auto_switch_source_on_failure", False))

    def _try_auto_switch_source_after_failure(self) -> bool:
        if self.session is None or not self._should_auto_switch_source_after_failure():
            return False
        if self._startup_state.stage is PlaybackStartupStage.PLAYING:
            return False
        current_key = self._current_source_attempt_key()
        next_key = self._next_source_after_failure()
        if current_key is None or next_key is None:
            return False
        if current_key in self._auto_switched_failure_sources:
            return False
        self._auto_switched_failure_sources.add(current_key)
        self._append_log("播放失败，自动切换线路")
        self._switch_active_source(*next_key, reset_auto_switch_state=False)
        return True

    def _retry_failed_startup(self) -> None:
        self._reset_auto_switched_failure_sources()
        self._replay_current_item()

    def _switch_line_after_failure(self) -> None:
        next_source = self._next_source_after_failure()
        if next_source is None:
            return
        self._switch_active_source(*next_source)

    def _switch_parser_after_failure(self) -> None:
        if not self._current_item_requires_parse():
            return
        if self.parse_combo.count() <= 2:
            return
        current_index = max(1, self.parse_combo.currentIndex())
        next_index = current_index + 1
        if next_index >= self.parse_combo.count():
            next_index = 1
        if next_index == current_index:
            return
        self.parse_combo.setCurrentIndex(next_index)

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

    def _external_metadata_link_html(self, url: str, label: str) -> str:
        return external_link_html(url, label)

    def _global_search_url(self, keyword: str) -> QUrl:
        url = QUrl("atv-player://global-search")
        query = QUrlQuery()
        query.addQueryItem("keyword", keyword)
        url.setQuery(query)
        return url

    def _global_search_link_html(self, keyword: object) -> str:
        text = str(keyword or "").strip()
        if not text:
            return ""
        href = html.escape(self._global_search_url(text).toString())
        accent = current_theme_manager().tokens_for(current_resolved_theme()).accent
        return (
            f'<a href="{href}" style=" text-decoration:none; '
            f'color:{accent}; font-weight:600;">'
            f"{html.escape(text)}</a>"
        )

    def _external_metadata_url(self, vod: VodItem | None, label: str, value: object, target: str = "") -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith(("http://", "https://")):
            return text

        normalized_label = str(label or "").strip().lower()
        normalized_target = str(target or "").strip().lower()
        if normalized_target == "bilibili":
            if _BILIBILI_BVID_RE.match(text):
                return f"https://www.bilibili.com/video/{text}"
            ss_match = _BILIBILI_SS_ID_RE.match(text)
            if ss_match is not None:
                return f"https://www.bilibili.com/bangumi/play/ss{ss_match.group(1)}"
            season_match = _BILIBILI_SEASON_ID_RE.match(text)
            if season_match is not None:
                return f"https://www.bilibili.com/bangumi/play/ss{season_match.group(1)}"
            if normalized_label == "season id" and text.isdigit():
                return f"https://www.bilibili.com/bangumi/play/ss{text}"
        if normalized_target == "douban" or normalized_label in {"豆瓣id", "dbid"}:
            return f"https://movie.douban.com/subject/{text}/"
        if normalized_target == "bangumi" or normalized_label == "bangumi id":
            return f"https://bgm.tv/subject/{text}"
        if normalized_target == "imdb" or normalized_label == "imdb id":
            return f"https://www.imdb.com/title/{text}"
        if normalized_target in {"movie", "tv"}:
            return f"https://www.themoviedb.org/{normalized_target}/{text}"
        if normalized_target == "tmdb" or normalized_label == "tmdb id":
            media_type = "movie"
            if vod is not None:
                media_type = infer_tmdb_media_type(
                    MetadataQuery(
                        title=str(getattr(vod, "vod_name", "") or "").strip(),
                        year=str(getattr(vod, "vod_year", "") or "").strip(),
                        type_name=str(getattr(vod, "type_name", "") or "").strip(),
                        category_name=str(getattr(vod, "category_name", "") or "").strip(),
                    ),
                ) or "movie"
            return f"https://www.themoviedb.org/{media_type}/{text}"
        return ""

    def _metadata_action_url(self, action: PlaybackDetailFieldAction, label: str = "") -> QUrl:
        if action.type == "link":
            external_url = self._external_metadata_url(
                self.session.vod if self.session is not None else None,
                label,
                action.value,
                action.target,
            )
            if external_url:
                return QUrl(external_url)
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
        if action_target not in {"", "bilibili", "douban", "tmdb", "bangumi"}:
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

    def _metadata_row_html(self, vod: VodItem, label: str, value: object) -> str:
        text = str(value or "")
        if label == "名称":
            search_html = self._global_search_link_html(text)
            if search_html:
                return f"{html.escape(label)}: {search_html}".rstrip()
        url = self._external_metadata_url(vod, label, text)
        if url:
            return f"{html.escape(label)}: {self._external_metadata_link_html(url, text)}".rstrip()
        if "[a=cr:" not in text:
            trimmed = text.rstrip()
            leading_spaces = len(trimmed) - len(trimmed.lstrip(" "))
            value_html = html.escape(trimmed[leading_spaces:]).replace("\n", "<br>")
            if leading_spaces:
                value_html = ("&nbsp;" * leading_spaces) + value_html
            return f"{html.escape(label)}: {value_html}".rstrip()
        return f"{html.escape(label)}: {self._render_metadata_value_html(text)}".rstrip()

    def _detail_field_html(self, field: PlaybackDetailField) -> str:
        if self.session is not None and len(field.value_parts) == 1:
            part = field.value_parts[0]
            if part.action is None:
                url = self._external_metadata_url(self.session.vod, field.label, part.label)
                if url:
                    return f"{html.escape(field.label)}: {self._external_metadata_link_html(url, part.label)}".rstrip()
        parts: list[str] = []
        for part in field.value_parts:
            if part.action is None:
                parts.append(html.escape(part.label))
                continue
            action_url = self._metadata_action_url(part.action, field.label).toString()
            if action_url.startswith(("http://", "https://")):
                parts.append(self._external_metadata_link_html(action_url, part.label))
                continue
            href = html.escape(action_url)
            label = html.escape(part.label)
            parts.append(f'<a href="{href}">{label}</a>')
        return f"{html.escape(field.label)}: {' / '.join(parts)}".rstrip()

    def _handle_metadata_link(self, url: QUrl) -> None:
        if url.scheme() in {"http", "https"}:
            if not QDesktopServices.openUrl(url):
                self._append_log(f"详情跳转失败[link]: 无法打开链接 {url.toString()}")
            return
        if url.scheme() == "atv-player" and url.host() == "global-search":
            keyword = QUrlQuery(url).queryItemValue("keyword", QUrl.ComponentFormattingOption.FullyDecoded).strip()
            if keyword:
                self.global_search_requested.emit(keyword)
                self._return_to_main()
            return
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
        button.setProperty("icon_name", icon_name)
        if bool(button.property("use_tinted_icon")):
            role = str(button.property("control_role") or "secondary")
            tokens = current_theme_manager().player_tokens_for(current_resolved_theme())
            color = tokens.player_primary_button_icon if role == "primary" else tokens.player_button_icon
            icon = tint_icon(icon, color, size=button.iconSize())
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

    @staticmethod
    def _is_resize_cursor_shape(shape: Qt.CursorShape) -> bool:
        return shape in (
            Qt.CursorShape.SizeHorCursor,
            Qt.CursorShape.SizeVerCursor,
            Qt.CursorShape.SizeFDiagCursor,
            Qt.CursorShape.SizeBDiagCursor,
        )

    def _set_video_cursor_hidden(self, hidden: bool) -> None:
        cursor_shape = Qt.CursorShape.BlankCursor if hidden else Qt.CursorShape.ArrowCursor
        for widget in self._video_surface_widgets():
            widget.setCursor(cursor_shape)
        current_shape = self.cursor().shape()
        if self._is_resize_cursor_shape(current_shape):
            return
        if hidden and not self._video_pointer_inside:
            return
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

    def _apply_metadata_query_redirects_to_danmaku_titles(self, session) -> None:
        """用手动修正过的查询重定向预填弹幕搜索标题。

        网盘混淆目录名（如"X 心动的XH9"）作为弹幕搜索标题什么都搜不到；查询重
        定向行记录了用户在刮削对话框修正过的真实剧名，预填到 danmaku_search_title
        让自动搜索和弹幕源对话框直接用修正标题。"""
        repo = getattr(session, "metadata_binding_repository", None)
        load_redirect = getattr(repo, "load_query_redirect", None)
        if not callable(load_redirect):
            return
        year = str(getattr(session.vod, "vod_year", "") or "").strip()
        redirected_by_title: dict[str, str] = {}
        playlists = list(session.playlists or []) or [session.playlist]
        for playlist in playlists:
            for item in playlist:
                if item.danmaku_search_query_overridden:
                    continue
                base_title = str(item.media_title or "").strip() or str(session.vod.vod_name or "").strip()
                if not base_title:
                    continue
                if base_title not in redirected_by_title:
                    try:
                        redirect = load_redirect(base_title, year)
                    except Exception:
                        redirect = None
                    redirected_by_title[base_title] = redirect[0] if redirect else ""
                redirect_title = redirected_by_title[base_title]
                if not redirect_title:
                    continue
                current = str(item.danmaku_search_title or "").strip()
                if (
                    current
                    and current != redirect_title
                    and normalize_metadata_scrape_title(current) != normalize_metadata_scrape_title(base_title)
                ):
                    # 已有偏好/手动设置的其它标题，不覆盖
                    continue
                item.danmaku_search_title = redirect_title

    def open_session(self, session, start_paused: bool = False) -> None:
        self._pending_file_loaded_danmaku_item = None
        self._reset_auto_switched_failure_sources()
        self._ytdlp_full_resolve_recovery_item = None
        self._invalidate_play_item_resolution()
        if session.source_groups:
            session.playlists, mapping = self._flatten_source_groups(session.source_groups)
            if not session.playlists:
                session.playlists = [session.playlist]
                session.source_groups = self._build_source_groups_from_playlists(session.playlists)
                mapping = {(0, 0): 0}
            session.source_group_index = max(0, min(session.source_group_index, len(session.source_groups) - 1))
            active_group = session.source_groups[session.source_group_index]
            session.source_index = max(0, min(session.source_index, len(active_group.sources) - 1))
            session.playlist_index = mapping[(session.source_group_index, session.source_index)]
            session.playlist = session.playlists[session.playlist_index]
        else:
            if not session.playlists:
                session.playlists = [session.playlist]
                session.playlist_index = 0
            session.source_groups = self._build_source_groups_from_playlists(session.playlists)
            session.source_group_index = max(0, min(session.playlist_index, len(session.source_groups) - 1))
            session.source_index = 0
            session.playlist = session.playlists[session.playlist_index]
        self.session = session
        self._playlist_sort_state.reset(session.playlists)
        initial_item = session.playlist[session.start_index] if 0 <= session.start_index < len(session.playlist) else None
        if initial_item is not None:
            self._snapshot_item_detail_fields(initial_item)
        session.original_vod = self._build_original_metadata_snapshot(session.vod)
        session.show_original_metadata = False
        session.current_metadata_poster_index = 0
        self._metadata_scrape_binding_title = str(getattr(initial_item, "media_title", "") or session.vod.vod_name or "").strip()
        self._metadata_scrape_binding_year = str(session.vod.vod_year or "").strip()
        self._metadata_scrape_alias_binding_key = None
        self._apply_metadata_query_redirects_to_danmaku_titles(session)
        self._metadata_scrape_query_saved = False
        self._metadata_scrape_saved_title = ""
        self._metadata_scrape_saved_year = ""
        self._metadata_scrape_saved_category = ""
        self._metadata_scrape_saved_provider = ""
        self._bilibili_tree_flat_index_by_item_id = {}
        self._bilibili_tree_flat_index_by_group_item = {}
        self._bilibili_tree_group_item_by_flat_index = {}
        default_playlist_mode = "list"
        if (
            self._supports_bilibili_grouped_playlist_tree()
            and self.config is not None
            and getattr(self.config, "bilibili_grouped_playlist_tree_enabled", False)
        ):
            default_playlist_mode = "tree"
        self._playlist_panel_mode = default_playlist_mode
        self._playlist_panel_resume_mode = default_playlist_mode
        self.current_index = session.start_index
        if self._bilibili_grouped_playlist_tree_enabled():
            self._activate_bilibili_tree_playlist()
        current_title = (
            session.playlist[self.current_index].title
            if 0 <= self.current_index < len(session.playlist)
            else ""
        )
        logger.info(
            (
                "PlayerWindow open session vod_id=%s start_index=%s playlist_index=%s "
                "source_group_index=%s source_index=%s start_title=%s paused=%s"
            ),
            session.vod.vod_id,
            session.start_index,
            session.playlist_index,
            session.source_group_index,
            session.source_index,
            current_title,
            start_paused,
        )
        self.playlist_title_mode = "episode"
        self._install_danmaku_log_handler(session)
        self._render_poster()
        self._render_metadata()
        self._render_detail_fields()
        self._refresh_heat_summary_for_current_item()
        self._refresh_metadata_original_toggle()
        self._reset_log()
        self._start_metadata_hydration()
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
        self._sync_native_always_on_top(failure_message="播放时置顶同步失败")
        self._set_last_player_paused(start_paused)
        self._update_play_button_icon()
        self._refresh_window_title()
        self._render_playlist_source_combos()
        self._render_playlist_sort_combo()
        self._render_playlist_title_tabs()
        self._render_playlist_items()
        self._render_bilibili_playlist_tree()
        self._sync_playlist_panel_mode()
        self._render_detail_actions()
        self._refresh_danmaku_source_entry_points()
        self.progress.setValue(0)
        self.progress.set_buffer_value(0)
        self._clear_chapter_markers()
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
        self._start_episode_title_enhancement()
        self.report_timer.start()
        self.progress_timer.start()
        self._sync_video_cursor_autohide()

    def _maybe_restore_cached_danmaku_for_current_item(self, *, allow_with_playback_loader: bool = False) -> None:
        if self.session is None:
            return
        if self.session.playback_loader is not None and not allow_with_playback_loader:
            logger.info(
                "Skip cached danmaku restore during open because playback_loader is active index=%s title=%s",
                self.current_index,
                self.session.playlist[self.current_index].title if 0 <= self.current_index < len(self.session.playlist) else "",
            )
            return
        if not (0 <= self.current_index < len(self.session.playlist)):
            return
        current_item = self.session.playlist[self.current_index]
        if not str(current_item.url or "").strip():
            logger.info("Skip cached danmaku restore because current item has no url index=%s title=%s", self.current_index, current_item.title)
            return
        if current_item.danmaku_xml or current_item.danmaku_pending:
            logger.info(
                "Skip cached danmaku restore because danmaku already present index=%s title=%s has_xml=%s pending=%s",
                self.current_index,
                current_item.title,
                bool(current_item.danmaku_xml),
                current_item.danmaku_pending,
            )
            return
        controller = getattr(self.session, "danmaku_controller", None)
        if controller is None:
            return
        load_cached = getattr(controller, "load_cached_danmaku_sources", None)
        switch_source = getattr(controller, "switch_danmaku_source", None)
        if not callable(load_cached) or not callable(switch_source):
            return
        try:
            restored = bool(
                load_cached(
                    current_item,
                    playlist=self.session.playlist,
                    media_duration_seconds=self._current_media_duration_seconds(),
                )
            )
        except TypeError:
            restored = bool(load_cached(current_item))
        logger.info(
            "Attempt cached danmaku restore index=%s title=%s restored=%s selected_url=%s",
            self.current_index,
            current_item.title,
            restored,
            current_item.selected_danmaku_url,
        )
        if not restored:
            auto_resolve = getattr(controller, "auto_resolve_danmaku", None)
            if not callable(auto_resolve):
                return
            self._start_danmaku_source_task(
                current_item,
                error_prefix="弹幕自动下载失败",
                task=lambda: auto_resolve(
                    current_item,
                    playlist=self.session.playlist,
                    media_duration_seconds=self._current_media_duration_seconds(),
                ),
                configure_danmaku_on_success=True,
                debug_label="自动下载",
            )
            return
        selected_url = str(current_item.selected_danmaku_url or "").strip()
        if not selected_url or current_item.danmaku_xml or current_item.danmaku_pending:
            logger.info(
                "Skip cached danmaku download after restore index=%s title=%s selected_url=%s has_xml=%s pending=%s",
                self.current_index,
                current_item.title,
                selected_url,
                bool(current_item.danmaku_xml),
                current_item.danmaku_pending,
            )
            return
        self._start_danmaku_source_task(
            current_item,
            error_prefix="恢复缓存弹幕失败",
            task=lambda: switch_source(current_item, selected_url),
            configure_danmaku_on_success=True,
            debug_label="缓存恢复",
        )

    def _video_load(
        self,
        url: str,
        pause: bool = False,
        start_seconds: int = 0,
        headers: dict[str, str] | None = None,
        poster_image_path: str | None = None,
        audio_files: str = "",
        ytdl_format: str = "",
    ) -> None:
        extra_kwargs: dict[str, object] = {}
        normalized_headers = normalize_media_request_headers(url, headers)
        if normalized_headers:
            extra_kwargs["headers"] = normalized_headers
        if poster_image_path:
            extra_kwargs["poster_image_path"] = poster_image_path
        if audio_files:
            extra_kwargs["audio_files"] = audio_files
        if ytdl_format:
            extra_kwargs["ytdl_format"] = ytdl_format
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
        if load_result.source_groups:
            self._apply_drive_grouped_loader_result(load_result)
            return
        replacement = list(load_result.replacement_playlist)
        logger.info(
            "Apply playback loader replacement old_index=%s replacement_size=%s replacement_start_index=%s",
            self.current_index,
            len(replacement),
            load_result.replacement_start_index,
        )
        reset_prefetch = getattr(self.controller, "reset_next_episode_danmaku_prefetch_state", None)
        if callable(reset_prefetch):
            reset_prefetch(self.session)
        if self._bilibili_grouped_playlist_tree_enabled():
            group_index, _item_index = self._bilibili_tree_group_item_by_flat_index.get(
                self.current_index,
                (self.session.playlist_index, 0),
            )
            self.session.playlist_index = group_index
            self.session.source_group_index = group_index
            self.session.source_index = 0
        active_group = self.session.source_groups[self.session.source_group_index]
        active_source = active_group.sources[self.session.source_index]
        active_source.playlist = replacement
        self.session.playlists[self.session.playlist_index] = replacement
        self.session.playlist = replacement
        self.current_index = max(
            0,
            min(load_result.replacement_start_index, len(replacement) - 1),
        )
        replacement_item = replacement[self.current_index]
        self._playlist_sort_state.remember(replacement)
        self._playlist_sort_state.apply(replacement)
        self.current_index = find_playlist_item_index(
            replacement,
            replacement_item,
            self.current_index,
        )
        self.session.start_index = self.current_index
        if self._bilibili_grouped_playlist_tree_enabled():
            self._activate_bilibili_tree_playlist(
                preferred_group_item=(self.session.playlist_index, self.current_index)
            )
        self._render_playlist_source_combos()
        self._render_playlist_sort_combo()
        self.playlist_title_mode = "episode"
        self._render_playlist_title_tabs()
        self._render_playlist_items()
        self._render_bilibili_playlist_tree()
        self._sync_playlist_panel_mode()
        self._render_detail_actions()
        self.session.episode_titles_hydrated = False
        self._start_episode_title_enhancement()

    def _apply_drive_grouped_loader_result(self, load_result: PlaybackLoadResult) -> None:
        # A drive item may already live under plugin groups (for example 百度/夸克).
        # Keep those parent levels and attach the resolved directories below the selected
        # resource instead of replacing session.source_groups with the directory list.
        session = self.session
        if session is None:
            return
        source_groups = load_result.source_groups or []
        playlists = load_result.playlists or []
        if not source_groups or not playlists:
            return
        reset_prefetch = getattr(self.controller, "reset_next_episode_danmaku_prefetch_state", None)
        if callable(reset_prefetch):
            reset_prefetch(session)
        parent_groups = self._session_source_groups()
        parent_group_index = max(0, min(session.source_group_index, len(parent_groups) - 1)) if parent_groups else 0
        parent_source_index = 0
        parent_source: PlaybackSource | None = None
        if parent_groups and parent_groups[parent_group_index].sources:
            parent_source_index = max(0, min(session.source_index, len(parent_groups[parent_group_index].sources) - 1))
            parent_source = parent_groups[parent_group_index].sources[parent_source_index]
            parent_source.subgroups = source_groups
            parent_source.subgroup_index = 0
            parent_source.playlist = playlists[0]
            parent_source.drive_resource_id = load_result.drive_resource_id
            parent_source.drive_files_loader = load_result.drive_files_loader
            session.source_group_index = parent_group_index
            session.source_index = parent_source_index
        else:
            session.source_groups = source_groups
            session.playlists = playlists
            session.source_group_index = 0
            session.source_index = 0
        session.playlist_index = 0
        session.drive_resource_id = load_result.drive_resource_id
        session.drive_files_loader = load_result.drive_files_loader
        # Drive items already carry a playable proxy URL; clear any detail_resolver whose
        # get_detail(item.vod_id) would receive a raw path (not a playurl id) and 500.
        session.detail_resolver = None
        playlist = playlists[0]
        session.playlist = playlist
        if 0 <= session.playlist_index < len(session.playlists):
            session.playlists[session.playlist_index] = playlist
        if self._restore_nested_drive_history(parent_source):
            playlist = session.playlist
        elif playlist:
            self.current_index = max(0, min(load_result.replacement_start_index, len(playlist) - 1))
        else:
            self.current_index = 0
        self._playlist_sort_state.remember(playlist)
        self._playlist_sort_state.apply(playlist)
        session.start_index = self.current_index
        self.playlist_title_mode = "episode"
        self._render_playlist_source_combos()
        self._render_playlist_sort_combo()
        self._render_playlist_title_tabs()
        self._render_playlist_items()
        self._render_bilibili_playlist_tree()
        self._sync_playlist_panel_mode()
        self._render_detail_actions()
        session.episode_titles_hydrated = False
        self._start_episode_title_enhancement()

    def _restore_nested_drive_history(self, parent_source: PlaybackSource | None) -> bool:
        """Select the nested drive directory containing the saved history URL.

        A grouped drive resource retains the plugin/source indexes of its parent, so
        those indexes alone cannot identify a child directory.  Resolve each lazy
        directory once during initial restore and use the saved media URL as its
        stable identity.
        """
        session = self.session
        if session is None or parent_source is None:
            return False
        history = session.resume_history
        session.resume_history = None
        if history is None or not parent_source.subgroups:
            return False
        target_url = history.episode_url.strip()
        target_name = urlparse(target_url).path.rsplit("/", 1)[-1]

        def restore_subgroup(subgroup_index: int) -> bool:
            if not 0 <= subgroup_index < len(parent_source.subgroups):
                return False
            subgroup = parent_source.subgroups[subgroup_index]
            self._ensure_drive_subgroup_loaded(subgroup)
            if not subgroup.sources:
                return False
            playlist = subgroup.sources[0].playlist
            target_play_id = target_url
            parts = target_play_id.split("@")
            if len(parts) >= 4:
                target_play_id = "@".join(parts[:2])
            for index, item in enumerate(playlist):
                if item.play_id and item.play_id == target_play_id:
                    return self._select_nested_drive_history_item(
                        parent_source, subgroup_index, playlist, index
                    )
                if item.url.strip() == target_url:
                    return self._select_nested_drive_history_item(
                        parent_source, subgroup_index, playlist, index
                    )
            if 0 <= history.episode < len(playlist):
                return self._select_nested_drive_history_item(
                    parent_source, subgroup_index, playlist, history.episode
                )
            return False

        # 规范网盘路径优先:跨端记录的子目录名/播放 id 可能与本端解析结果对不上
        # (服务端线路名被公共前后缀裁剪、各资源文件命名不同),路径才是稳定内容指针。
        drive_path = str(getattr(history, "drive_path", "") or "")
        if drive_path and "/" in drive_path:
            dir_rel = drive_path.rsplit("/", 1)[0]
            for subgroup_index, subgroup in enumerate(parent_source.subgroups):
                _, subgroup_rel = split_drive_path(decode_drive_dir_id(subgroup.drive_dir_id))
                if subgroup_rel != dir_rel:
                    continue
                self._ensure_drive_subgroup_loaded(subgroup)
                if not subgroup.sources:
                    continue
                playlist = subgroup.sources[0].playlist
                for index, item in enumerate(playlist):
                    if item.path and drive_relative_path(item.path) == drive_path:
                        return self._select_nested_drive_history_item(
                            parent_source, subgroup_index, playlist, index
                        )

        if history.source_subgroup_name:
            for subgroup_index, subgroup in enumerate(parent_source.subgroups):
                if (
                    subgroup.label == history.source_subgroup_name
                    and restore_subgroup(subgroup_index)
                ):
                    return True
            if restore_subgroup(history.source_subgroup_index):
                return True

        if history.drive_dir_id:
            for subgroup_index, subgroup in enumerate(parent_source.subgroups):
                if subgroup.drive_dir_id == history.drive_dir_id:
                    return restore_subgroup(subgroup_index)
        # Records created before directory IDs were persisted fall back to URL
        # matching, which also preserves compatibility with existing histories.
        if not target_name:
            return False
        basename_matches: list[tuple[int, list[PlayItem], int]] = []
        for subgroup_index, subgroup in enumerate(parent_source.subgroups):
            self._ensure_drive_subgroup_loaded(subgroup)
            if not subgroup.sources:
                continue
            playlist = subgroup.sources[0].playlist
            for index, item in enumerate(playlist):
                item_url = item.url.strip()
                if item_url == target_url:
                    return self._select_nested_drive_history_item(
                        parent_source,
                        subgroup_index,
                        playlist,
                        index,
                    )
                if urlparse(item_url).path.rsplit("/", 1)[-1] == target_name:
                    basename_matches.append((subgroup_index, playlist, index))
        if len(basename_matches) == 1:
            subgroup_index, playlist, matched_index = basename_matches[0]
            return self._select_nested_drive_history_item(
                parent_source,
                subgroup_index,
                playlist,
                matched_index,
            )
        return False

    def _select_nested_drive_history_item(
        self,
        parent_source: PlaybackSource,
        subgroup_index: int,
        playlist: list[PlayItem],
        episode_index: int,
    ) -> bool:
        if self.session is None:
            return False
        parent_source.subgroup_index = subgroup_index
        self.session.playlist = playlist
        self.session.start_index = episode_index
        self.current_index = episode_index
        logger.info(
            "Restored nested drive directory vod_id=%s subgroup_index=%s episode_index=%s",
            self.session.vod.vod_id,
            subgroup_index,
            episode_index,
        )
        return True

    def _start_playback_loader(
        self,
        *,
        previous_index: int,
        start_position_seconds: int,
        pause: bool,
        hydrate_only: bool = False,
    ) -> None:
        if self.session is None or self.session.playback_loader is None:
            return
        current_item = self.session.playlist[self.current_index]
        if not hydrate_only:
            self._set_startup_state(self._startup_coordinator.resolving(self._resolving_startup_message(current_item)))
        playback_loader = self.session.playback_loader
        youtube_detail_parse = self._is_youtube_playback_loader_item(current_item)
        if youtube_detail_parse:
            self._append_log("详情解析中", dedupe=True)
        if not hydrate_only:
            self._append_log(f"正在加载播放地址: {current_item.title}")
        self._playback_loader_request_id += 1
        request_id = self._playback_loader_request_id
        pending_loader = _PendingPlaybackLoader(
            index=self.current_index,
            previous_index=previous_index,
            start_position_seconds=start_position_seconds,
            pause=pause,
            hydrate_only=hydrate_only,
            youtube_detail_parse=youtube_detail_parse,
            intent_generation=self._playback_intent_generation,
        )
        if hydrate_only:
            # Snapshot the playing state so a failed quality upgrade can roll back.
            pending_loader.playback_started_url = str(current_item.url or "")
            pending_loader.playback_started_audio_url = str(current_item.audio_url or "")
            pending_loader.playback_started_headers = (
                dict(current_item.headers) if current_item.headers else None
            )
            pending_loader.playback_started_quality_id = str(current_item.selected_playback_quality_id or "")
        self._pending_playback_loader = pending_loader

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

    def _should_preload_ytdlp_passthrough(self, item: PlayItem) -> bool:
        if not self._is_youtube_page_url(str(item.url or "").strip()):
            return False
        if not str(item.ytdl_format or "").strip():
            return False
        return not (item.playback_qualities or item.audio_tracks or item.external_subtitles)

    def _is_youtube_resolved_direct_item(self, item: PlayItem) -> bool:
        source_url = str(item.original_url or "").strip()
        resolved_url = str(item.url or "").strip()
        return bool(
            source_url
            and resolved_url
            and self._is_youtube_page_url(source_url)
            and not self._is_youtube_page_url(resolved_url)
        )

    def _should_delay_ytdlp_metadata_hydration(self, item: PlayItem) -> bool:
        if not self._is_youtube_resolved_direct_item(item):
            return False
        return not item.playback_qualities

    def _schedule_ytdlp_metadata_hydration(self, *, expected_item: PlayItem, previous_index: int) -> None:
        self._pending_ytdlp_metadata_hydration = (expected_item, previous_index)

    def _start_pending_ytdlp_metadata_hydration_if_current(self) -> None:
        pending = self._pending_ytdlp_metadata_hydration
        if pending is None:
            return
        expected_item, previous_index = pending
        if self.session is None or not (0 <= self.current_index < len(self.session.playlist)):
            self._pending_ytdlp_metadata_hydration = None
            return
        if self.session.playlist[self.current_index] is not expected_item:
            self._pending_ytdlp_metadata_hydration = None
            return
        if self._pending_playback_loader is not None:
            return
        self._pending_ytdlp_metadata_hydration = None
        self._start_playback_loader(
            previous_index=previous_index,
            start_position_seconds=0,
            pause=False,
            hydrate_only=True,
        )

    def _maybe_upgrade_ytdlp_playback_quality(
        self,
        current_item: PlayItem,
        pending_loader: _PendingPlaybackLoader,
    ) -> bool:
        """Switch to the hydrated yt-dlp DASH/HLS stream when it beats the playing quality.

        After a fast low-quality start, metadata hydration replaces the item URL
        with the full yt-dlp result while mpv keeps playing the old stream. Reload
        at the current position to actually play the higher-quality stream.
        """
        if self.session is None or not (0 <= self.current_index < len(self.session.playlist)):
            return False
        if self.session.playlist[self.current_index] is not current_item:
            return False
        if not self._is_youtube_resolved_direct_item(current_item):
            return False
        if getattr(current_item, "is_live", False):
            return False
        started_url = str(pending_loader.playback_started_url or "")
        if started_url and str(current_item.url or "").strip() == started_url:
            return False
        pending_prepare = self._pending_playback_prepare
        if pending_prepare is not None and pending_prepare.index == self.current_index:
            return False
        target_height = _ytdlp_quality_height(str(current_item.selected_playback_quality_id or ""))
        if target_height is None:
            return False
        current_video_height = getattr(self.video_widget, "current_video_height", None)
        try:
            playing_height = current_video_height() if callable(current_video_height) else None
        except Exception:
            playing_height = None
        if not playing_height or playing_height >= target_height:
            return False
        try:
            position_seconds = int(self.video.position_seconds() or 0)
        except Exception:
            position_seconds = 0
        logger.info(
            "Upgrading ytdlp playback quality index=%s %sp->%sp position=%ss url=%s",
            self.current_index,
            playing_height,
            target_height,
            position_seconds,
            _summarize_media_url(current_item.url),
        )
        self._append_log(f"清晰度提升: {playing_height}p → {target_height}p")
        try:
            self._start_current_item_playback(
                start_position_seconds=position_seconds,
                pause=not self.is_playing,
            )
        except Exception as exc:
            current_item.url = started_url or current_item.url
            current_item.audio_url = str(pending_loader.playback_started_audio_url or "")
            if pending_loader.playback_started_headers is not None:
                current_item.headers = dict(pending_loader.playback_started_headers)
            if pending_loader.playback_started_quality_id:
                current_item.selected_playback_quality_id = pending_loader.playback_started_quality_id
            self._refresh_video_quality_state()
            self._append_log(f"清晰度切换失败: {exc}")
            try:
                self._start_current_item_playback(
                    start_position_seconds=position_seconds,
                    pause=not self.is_playing,
                )
            except Exception:
                logger.warning("Failed to restore playback after quality upgrade failure", exc_info=True)
        return True

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
            if self.session.async_playback_loader:
                if self._should_preload_ytdlp_passthrough(current_item):
                    self._start_playback_loader(
                        previous_index=previous_index,
                        start_position_seconds=start_position_seconds,
                        pause=pause,
                        hydrate_only=False,
                    )
                    return False
                has_playable_url = bool(current_item.url) and not self._is_unresolved_ytdlp_page_item(current_item)
                if has_playable_url and self._should_delay_ytdlp_metadata_hydration(current_item):
                    self._schedule_ytdlp_metadata_hydration(
                        expected_item=current_item,
                        previous_index=previous_index,
                    )
                else:
                    self._start_playback_loader(
                        previous_index=previous_index,
                        start_position_seconds=start_position_seconds,
                        pause=pause,
                        hydrate_only=has_playable_url,
                    )
                if not has_playable_url:
                    return False
            else:
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

    def _is_unresolved_ytdlp_page_item(self, item: PlayItem) -> bool:
        if not self._is_youtube_page_url(str(item.url or "").strip()):
            return False
        if str(item.ytdl_format or "").strip():
            return False
        if str(item.selected_playback_quality_id or "").startswith("ytdlp_"):
            return False
        return True

    def _is_youtube_page_url(self, url: str) -> bool:
        parsed = urlparse(str(url or "").strip())
        hostname = (parsed.hostname or "").lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname in {"youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"}

    def _is_youtube_playback_loader_item(self, item: PlayItem) -> bool:
        if self.session is not None:
            if str(getattr(self.session, "source_kind", "") or "").strip().lower() == "youtube":
                return True
            for vod in (self.session.vod, getattr(self.session, "original_vod", None)):
                if getattr(vod, "detail_style", "") == "youtube":
                    return True
        for value in (item.original_url, item.url, item.vod_id, item.path):
            normalized = str(value or "").strip()
            if not normalized:
                continue
            lowered = normalized.lower()
            if lowered.startswith(("yt:video:", "yt:channel:", "yt:playlist:")):
                return True
            if self._is_youtube_page_url(normalized) or looks_like_youtube_video_id(normalized):
                return True
        return False

    def _start_current_item_playback(self, start_position_seconds: int = 0, pause: bool = False) -> None:
        if self.session is None:
            return
        self._set_startup_state(self._startup_coordinator.connecting())
        current_item = self.session.playlist[self.current_index]
        defer_post_load_configuration = self._should_defer_post_load_player_configuration()
        self._append_log(f"当前播放: {playlist_item_display_title(current_item, 'episode')}")
        self._append_log(f"播放地址: {current_item.url}")
        if start_position_seconds > self.opening_spin.value():
            effective_start_seconds = start_position_seconds
        else:
            effective_start_seconds = self.opening_spin.value()
        poster_image_path = self._preferred_audio_cover_path() if self._should_use_audio_cover(current_item.url) else None
        if defer_post_load_configuration:
            self._pending_post_load_item = current_item
            self._pending_post_load_pause = pause
        playback_ytdl_format = current_item.ytdl_format
        if self._is_youtube_resolved_direct_item(current_item):
            playback_ytdl_format = ""
        logger.info(
            "PlayerWindow start playback index=%s quality=%s ytdl_format=%s url=%s audio=%s start=%s pause=%s subtitles=%s",
            self.current_index,
            current_item.selected_playback_quality_id,
            playback_ytdl_format,
            _summarize_media_url(current_item.url),
            _summarize_media_url(current_item.audio_url),
            effective_start_seconds,
            pause,
            len(current_item.external_subtitles),
        )
        if self.video is self.video_widget:
            self._pending_file_loaded_danmaku_item = current_item
        try:
            self._video_load(
                current_item.url,
                pause=pause,
                start_seconds=effective_start_seconds,
                headers=current_item.headers,
                poster_image_path=poster_image_path,
                audio_files=current_item.audio_url,
                ytdl_format=playback_ytdl_format,
            )
        except Exception:
            if self._pending_file_loaded_danmaku_item is current_item:
                self._pending_file_loaded_danmaku_item = None
            if defer_post_load_configuration:
                self._pending_post_load_item = None
                self._pending_post_load_pause = False
            raise
        self._auto_advance_locked = False
        self._configure_video_surface_widgets()
        if defer_post_load_configuration:
            self._reset_subtitle_combo()
            self._reset_audio_combo()
        else:
            self._apply_post_load_player_configuration(current_item)
        if self.session is not None:
            self.controller.on_item_started(self.session, self.current_index)

    def _uses_event_driven_track_refresh(self) -> bool:
        return self.video is self.video_widget

    def _should_defer_post_load_player_configuration(self) -> bool:
        return False

    def _schedule_window_single_shot(self, delay_ms: int, callback: Callable[[], None]) -> None:
        if not self._can_deliver_async_result():
            return
        timer = QTimer(self)
        timer.setSingleShot(True)

        def run() -> None:
            timer.deleteLater()
            if self._can_deliver_async_result():
                callback()

        timer.timeout.connect(run)
        timer.start(max(0, int(delay_ms)))

    def _apply_post_load_player_configuration(self, current_item: PlayItem) -> None:
        if self._pending_post_load_pause:
            self._pending_post_load_pause = False
            self.controls.pause()
        self.controls.set_speed(self.current_speed)
        self.controls.set_volume(self.volume_slider.value())
        self._apply_muted_state()
        if self._uses_event_driven_track_refresh():
            self._reset_subtitle_combo()
        else:
            self._refresh_subtitle_state()
        self._schedule_followup_subtitle_refresh_if_needed(current_item)
        if self._uses_event_driven_track_refresh():
            self._reset_audio_combo()
        else:
            self._refresh_audio_state()
        self._refresh_video_quality_state()
        self._configure_danmaku_for_current_item()

    def refresh_runtime_video_output_settings(self) -> None:
        if hasattr(self.video_widget, "apply_runtime_video_output_settings"):
            self.video_widget.apply_runtime_video_output_settings()

    def _handle_video_file_loaded(self) -> None:
        self._refresh_chapter_markers()
        self._schedule_window_single_shot(1500, self._start_pending_ytdlp_metadata_hydration_if_current)
        pending_danmaku_item = self._pending_file_loaded_danmaku_item
        self._pending_file_loaded_danmaku_item = None
        pending_item = self._pending_post_load_item
        if (
            pending_danmaku_item is not None
            and pending_item is not pending_danmaku_item
            and self.session is not None
            and 0 <= self.current_index < len(self.session.playlist)
            and self.session.playlist[self.current_index] is pending_danmaku_item
        ):
            self._configure_danmaku_for_current_item()
        if pending_item is None:
            return

        def apply_if_still_current() -> None:
            if self.session is None:
                self._pending_post_load_item = None
                self._pending_post_load_pause = False
                return
            if not (0 <= self.current_index < len(self.session.playlist)):
                self._pending_post_load_item = None
                self._pending_post_load_pause = False
                return
            if self.session.playlist[self.current_index] is not pending_item:
                return
            self._pending_post_load_item = None
            self._apply_post_load_player_configuration(pending_item)

        self._schedule_window_single_shot(0, apply_if_still_current)

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

        self._schedule_window_single_shot(150, refresh_if_still_current)

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
        self._ensure_drive_group_loaded(self.session.source_group_index)
        if not self.session.playlist:
            self._append_log("加载目录失败，请在目录下拉框中选择其他目录")
            return
        if self.current_index >= len(self.session.playlist):
            self.current_index = max(0, len(self.session.playlist) - 1)
        self._reset_playback_observation()
        self._ignore_playback_finished_until = 0.0
        self._recent_user_seek_target_seconds = None
        self._set_startup_state(self._startup_coordinator.preparing())
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
        current_item = self.session.playlist[self.current_index]
        if not current_item.url:
            if self._try_auto_switch_source_after_failure():
                return
            self._show_failed_startup_state(f"播放失败: 没有可用的播放地址: {current_item.title}")
            self._mark_playback_stopped()
            self._append_log(f"播放失败: 没有可用的播放地址: {current_item.title}")
            return
        self._refresh_parse_combo_enabled_state()
        self._start_current_item_playback(start_position_seconds=start_position_seconds, pause=pause)
        # Danmaku lookup can take tens of seconds (external providers, no match for short
        # dramas), and it shares the serial controller-task queue — so kick it off only
        # after playback has started, never before.
        self._maybe_restore_cached_danmaku_for_current_item()

    def _format_metadata_text(self, vod) -> str:
        if getattr(vod, "detail_style", "") == "youtube":
            lines = [f"标题: {vod.vod_name}".rstrip()]
            lines.extend(self._detail_field_plain_text(field) for field in self._current_detail_fields())
            return "\n".join(lines)
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
            rows = [(label, value) for label, value in rows if self._bilibili_metadata_row_has_value(value)]
        lines = [f"{label}: {value}".rstrip() for label, value in rows]
        lines.extend(self._detail_field_plain_text(field) for field in self._current_detail_fields())
        lines.append("")
        lines.append("简介:")
        lines.append(vod.vod_content)
        return "\n".join(lines)

    def _format_metadata_html(self, vod) -> str:
        if getattr(vod, "detail_style", "") == "youtube":
            parts = [self._metadata_row_html(vod, "标题", vod.vod_name)]
            parts.extend(self._detail_field_html(field) for field in self._current_detail_fields())
            return "<br>".join(parts)
        if getattr(vod, "detail_style", "") == "live":
            if getattr(vod, "epg_current", ""):
                parts = [html.escape("当前节目:"), self._render_metadata_value_html(vod.epg_current)]
                if getattr(vod, "epg_schedule", ""):
                    parts.extend(["", html.escape("今日节目单:"), self._render_metadata_value_html(vod.epg_schedule)])
                parts.extend(self._detail_field_html(field) for field in self._current_detail_fields())
                return "<br>".join(parts)
            rows = [
                ("标题", vod.vod_name),
                ("平台", vod.vod_director),
                ("类型", vod.type_name),
                ("主播", vod.vod_actor),
                ("人气", vod.vod_remarks),
            ]
            parts = [self._metadata_row_html(vod, label, value) for label, value in rows]
            parts.extend(self._detail_field_html(field) for field in self._current_detail_fields())
            return "<br>".join(parts)
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
            rows = [(label, value) for label, value in rows if self._bilibili_metadata_row_has_value(value)]
        parts = [self._metadata_row_html(vod, label, value) for label, value in rows]
        parts.extend(self._detail_field_html(field) for field in self._current_detail_fields())
        parts.append("")
        parts.append(html.escape("简介:"))
        parts.append(self._render_metadata_value_html(vod.vod_content))
        return "<br>".join(parts)

    def _bilibili_metadata_row_has_value(self, value: object) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return bool(text.strip(" /|、,，"))

    def _render_metadata(self) -> None:
        vod = self._current_metadata_vod()
        if vod is None:
            self.metadata_view.clear()
            return
        self.metadata_view.setHtml(self._format_metadata_html(vod))

    def _apply_resolved_vod(self, resolved_vod: VodItem) -> None:
        if self.session is None:
            return
        self.session.vod = resolved_vod
        self._update_original_metadata_snapshot(resolved_vod)
        self._reset_metadata_poster_index()
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
        # Lazy-load the active drive directory before touching the playlist (covers the case
        # where the build-time eager load failed, e.g. a transient backend/driver error).
        self._ensure_drive_group_loaded(self.session.source_group_index)
        playlist = self.session.playlist
        if not playlist:
            self._append_log("加载目录失败，请在目录下拉框中选择其他目录")
            return
        if index >= len(playlist):
            index = max(0, len(playlist) - 1)
        previous_index = self.current_index
        previous_detail_poster_source = self._preferred_detail_poster_source()
        previous_video_poster_source = self._preferred_video_poster_source()
        if previous_index != index:
            reset_prefetch = getattr(self.controller, "reset_next_episode_danmaku_prefetch_state", None)
            if callable(reset_prefetch):
                reset_prefetch(self.session)
        self.current_index = index
        self._sync_danmaku_offset_controls(self.session.playlist[self.current_index])
        self._sync_bilibili_tree_active_group_from_current_index()
        try:
            self.playlist.setCurrentRow(self.current_index)
            self._sync_playlist_item_styles()
            self._sync_bilibili_tree_item_styles()
            self._refresh_danmaku_source_entry_points()
            self._render_metadata()
            self._render_detail_fields()
            if previous_index != index:
                self._refresh_heat_summary_for_current_item()
            self._render_detail_actions()
            if (
                previous_detail_poster_source != self._preferred_detail_poster_source()
                or previous_video_poster_source != self._preferred_video_poster_source()
            ):
                self._reset_metadata_poster_index()
                self._render_poster()
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

    def _load_poster_pixmap(self, source: str) -> QPixmap:
        source_path = self._local_poster_source_path(source)
        if source_path is None:
            return QPixmap()
        pixmap = QPixmap(str(source_path))
        if pixmap.isNull():
            return QPixmap()
        return pixmap.scaled(
            self._POSTER_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _load_video_poster_pixmap(self, source: str) -> QPixmap:
        source_path = self._local_poster_source_path(source)
        if source_path is None:
            return QPixmap()
        pixmap = QPixmap(str(source_path))
        if pixmap.isNull():
            return QPixmap()
        return pixmap

    def _video_poster_load_size(self) -> QSize:
        candidates = [self.video_stack.size(), self.size()]
        video_size = getattr(self.video, "size", None)
        if callable(video_size):
            candidates.insert(1, video_size())
        for candidate in candidates:
            if candidate.width() > self._POSTER_SIZE.width() and candidate.height() > self._POSTER_SIZE.height():
                return candidate
        return self._VIDEO_POSTER_LOAD_FALLBACK_SIZE

    def _start_poster_load(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        image_url = normalize_poster_url(source)
        if not image_url:
            return
        target_size = self._POSTER_SIZE if target != "video" else self._video_poster_load_size()

        def load() -> None:
            image = load_remote_poster_image(
                image_url,
                target_size,
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
            return
        pixmap = QPixmap.fromImage(image)
        self.poster_label.setText("")
        self.poster_label.setPixmap(pixmap)
        video_source = self._preferred_video_poster_source()
        if video_source == self._preferred_detail_poster_source():
            self._show_video_poster_overlay(pixmap)
            self._attach_audio_cover_if_available()
        self._refresh_poster_preview()

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
            source = str(loader() or "")
        except Exception:
            self._default_video_cover_source = ""
        else:
            normalized = normalize_poster_url(source)
            if source and not normalized:
                logger.info("Ignore unsupported default video cover source=%s", source)
                self._default_video_cover_source = ""
            else:
                self._default_video_cover_source = source
        return self._default_video_cover_source

    def _preferred_detail_poster_source(self) -> str:
        sources = self._current_metadata_poster_sources()
        if self.session is None or not sources:
            return ""
        index = self.session.current_metadata_poster_index % len(sources)
        return sources[index]

    def _current_metadata_poster_sources(self) -> list[str]:
        vod = self._current_metadata_vod()
        if vod is None:
            return []
        if getattr(vod, "detail_style", "") == "youtube":
            current_item = self._current_play_item()
            item_cover = str(getattr(current_item, "video_cover_override", "") or "").strip()
            if item_cover:
                return [item_cover]
        candidates = [str(source or "").strip() for source in vod.poster_candidates if str(source or "").strip()]
        if candidates:
            return candidates
        fallback = str(vod.vod_pic or "").strip()
        return [fallback] if fallback else []

    def _refresh_poster_navigation(self) -> None:
        visible = len(self._current_metadata_poster_sources()) > 1
        self._poster_previous_button.setHidden(not visible)
        self._poster_next_button.setHidden(not visible)

    def _step_metadata_poster(self, offset: int) -> None:
        if self.session is None:
            return
        sources = self._current_metadata_poster_sources()
        if len(sources) <= 1:
            self.session.current_metadata_poster_index = 0
            self._refresh_poster_navigation()
            self._refresh_poster_preview_navigation()
            return
        self.session.current_metadata_poster_index = (self.session.current_metadata_poster_index + offset) % len(sources)
        self._refresh_poster_navigation()
        self._render_detail_poster()
        self._refresh_poster_preview()

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

    def _should_defer_same_source_video_poster_load(self) -> bool:
        if self.session is None:
            return False
        if not self.session.async_playback_loader or self.session.playback_loader is None:
            return False
        current_item = self._current_play_item()
        return current_item is not None and not bool(current_item.url)

    def _should_use_audio_cover(self, url: str) -> bool:
        normalized_path = urlparse(url or "").path.lower()
        return any(normalized_path.endswith(suffix) for suffix in self._AUDIO_ONLY_SUFFIXES)

    def _local_poster_source_path(self, source: str) -> Path | None:
        normalized = normalize_poster_url(source)
        if not normalized or normalized.startswith(("http://", "https://", "data:")):
            return None
        source_path = Path(normalized)
        try:
            if not source_path.is_file():
                return None
        except OSError:
            return None
        return source_path

    def _preferred_audio_cover_path(self) -> str | None:
        source = self._preferred_video_poster_source().strip()
        if not source:
            return None
        source_path = self._local_poster_source_path(source)
        if source_path is not None:
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

    def _poster_preview_pixmap(self, source: str) -> QPixmap:
        source_path = self._local_poster_source_path(source)
        if source_path is None:
            normalized = normalize_poster_url(source)
            if normalized.startswith(("http://", "https://")):
                cached_path = poster_cache_path(normalized)
                if cached_path.is_file():
                    source_path = cached_path
        if source_path is not None:
            pixmap = QPixmap(str(source_path))
            if not pixmap.isNull():
                return pixmap
        current_pixmap = self.poster_label.pixmap()
        if current_pixmap is not None and not current_pixmap.isNull():
            return current_pixmap
        return QPixmap()

    def _open_detail_poster_preview(self) -> None:
        source = self._preferred_detail_poster_source()
        if not source:
            return
        pixmap = self._poster_preview_pixmap(source)
        if pixmap.isNull():
            return
        dialog = PosterPreviewDialog(pixmap, parent=self)
        dialog.setModal(True)
        self._poster_preview_dialog = dialog
        self._poster_preview_label = dialog.preview_label
        self._poster_preview_previous_button = dialog.previous_button
        self._poster_preview_next_button = dialog.next_button
        dialog.previous_button.clicked.connect(lambda: self._step_metadata_poster(-1))
        dialog.next_button.clicked.connect(lambda: self._step_metadata_poster(1))
        self._refresh_poster_preview_navigation()
        dialog.finished.connect(lambda _result, preview_dialog=dialog: self._clear_poster_preview(preview_dialog))
        dialog.show()

    def _clear_poster_preview(self, dialog: QDialog) -> None:
        if self._poster_preview_dialog is not dialog:
            return
        self._poster_preview_dialog = None
        self._poster_preview_label = None
        self._poster_preview_previous_button = None
        self._poster_preview_next_button = None

    def _refresh_poster_preview_navigation(self) -> None:
        visible = len(self._current_metadata_poster_sources()) > 1
        if self._poster_preview_previous_button is not None:
            self._poster_preview_previous_button.setHidden(not visible)
        if self._poster_preview_next_button is not None:
            self._poster_preview_next_button.setHidden(not visible)

    def _refresh_poster_preview(self) -> None:
        dialog = self._poster_preview_dialog
        if not isinstance(dialog, PosterPreviewDialog):
            return
        self._refresh_poster_preview_navigation()
        pixmap = self._poster_preview_pixmap(self._preferred_detail_poster_source())
        if pixmap.isNull():
            dialog.preview_label.setPixmap(QPixmap())
            return
        dialog.set_source_pixmap(pixmap)

    def _render_detail_poster(self) -> None:
        self._poster_request_id += 1
        if self.session is None:
            self.poster_label.clear()
            self.poster_label.setText("")
            self.poster_label.setPixmap(QPixmap())
            return
        source = self._preferred_detail_poster_source()
        if not source:
            self.poster_label.clear()
            self.poster_label.setText("")
            self.poster_label.setPixmap(QPixmap())
            return
        pixmap = self._load_poster_pixmap(source)
        if not pixmap.isNull():
            self.poster_label.setText("")
            self.poster_label.setPixmap(pixmap)
            return
        self.poster_label.clear()
        self.poster_label.setText("")
        self.poster_label.setPixmap(QPixmap())
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
        pixmap = self._load_video_poster_pixmap(source)
        if not pixmap.isNull():
            self._show_video_poster_overlay(pixmap)
            return
        detail_source = self._preferred_detail_poster_source()
        if source == detail_source:
            pixmap = self.poster_label.pixmap()
            if pixmap is not None and not pixmap.isNull():
                self._show_video_poster_overlay(pixmap)
            else:
                self._clear_video_poster_overlay()
                if self._should_defer_same_source_video_poster_load():
                    return
            if self._should_defer_same_source_video_poster_load():
                return
            self._start_poster_load(source, self._video_poster_request_id, target="video")
            return
        pixmap = self._load_poster_pixmap(source)
        if not pixmap.isNull():
            self._show_video_poster_overlay(pixmap)
            return
        self._clear_video_poster_overlay()
        self._start_poster_load(source, self._video_poster_request_id, target="video")

    def _render_poster(self) -> None:
        self._refresh_poster_navigation()
        self._render_detail_poster()
        self._render_video_poster()

    def _handle_video_picture_state_changed(self, state: str) -> None:
        self._video_picture_state = state
        if state == "loading":
            self._set_startup_state(self._startup_coordinator.buffering())
        elif state in {"visible", "audio-cover"}:
            self._set_startup_state(self._startup_coordinator.playing())
            self._reset_auto_switched_failure_sources()
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
        if self._should_recover_recent_seek_failure():
            self._append_log(message)
            self._recover_current_item_after_seek()
            return
        if self._try_recover_youtube_playback_with_full_resolve(message):
            return
        if self._try_auto_switch_source_after_failure():
            return
        self._show_failed_startup_state(message)
        self._mark_playback_stopped()
        self._append_log(message)
        self._video_surface_ready = False
        pixmap = self.video_poster_overlay.pixmap()
        if pixmap is not None and not pixmap.isNull():
            self._show_video_poster_overlay(pixmap)

    def _try_recover_youtube_playback_with_full_resolve(self, message: str) -> bool:
        """When a fast-started low-quality stream dies, restart with the yt-dlp full result.

        The full resolve races in the background from playback start; rerunning the
        playback loader consumes it (waiting while it is still in flight) and restarts
        playback with the DASH/HLS stream instead of failing outright. Only attempted
        once per item so a genuinely broken stream still reaches the failure UI.
        """
        if self.session is None or self.session.playback_loader is None:
            return False
        if not (0 <= self.current_index < len(self.session.playlist)):
            return False
        current_item = self.session.playlist[self.current_index]
        if not self._is_youtube_resolved_direct_item(current_item):
            return False
        if getattr(current_item, "is_live", False):
            return False
        if self._ytdlp_full_resolve_recovery_item is current_item:
            return False
        pending_loader = self._pending_playback_loader
        if pending_loader is not None:
            if not pending_loader.hydrate_only or pending_loader.index != self.current_index:
                return False
            # Drop the stale hydration wait; the racing resolve caches its result
            # anyway, and the recovery loader below picks it up.
            self._playback_loader_request_id += 1
            self._pending_playback_loader = None
        pending_prepare = self._pending_playback_prepare
        if pending_prepare is not None and pending_prepare.index == self.current_index:
            return False
        try:
            position_seconds = int(self.video.position_seconds() or 0)
        except Exception:
            position_seconds = 0
        self._ytdlp_full_resolve_recovery_item = current_item
        logger.info(
            "Recovering failed ytdlp fast playback with full resolve index=%s position=%ss error=%s",
            self.current_index,
            position_seconds,
            message,
        )
        self._append_log("低画质流播放失败，正在切换到 yt-dlp 完整解析...")
        self._start_playback_loader(
            previous_index=self.current_index,
            start_position_seconds=position_seconds,
            pause=False,
            hydrate_only=False,
        )
        return True

    def _should_recover_recent_seek_failure(self) -> bool:
        target_seconds = self._recent_user_seek_target_seconds
        if (
            target_seconds is None
            or time.monotonic() >= self._ignore_playback_finished_until
        ):
            return False
        try:
            duration = int(self.video.duration_seconds() or 0)
        except Exception:
            duration = 0
        if duration <= 0:
            return True
        ending_seconds = self.ending_spin.value() if hasattr(self, "ending_spin") else 0
        end_margin = max(2, int(ending_seconds or 0))
        return int(target_seconds) + end_margin < duration

    def _reset_log(self) -> None:
        self.log_view.clear()
        self._last_log_message = None

    def _format_log_line(self, message: str) -> str:
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        return f"[{timestamp}] {message}"

    def _logging_enabled(self) -> bool:
        return bool(getattr(self.config, "logging_enabled", True))

    def _current_log_context(self) -> dict[str, str | int]:
        session = self.session
        if session is None:
            return {
                "vod_id": "",
                "vod_name": "",
                "episode_title": "",
                "session_id": "",
                "source_group_index": -1,
                "source_index": -1,
                "playlist_index": -1,
                "url_summary": "",
            }
        playlist_index = self.current_index if 0 <= self.current_index < len(session.playlist) else -1
        current_item = session.playlist[playlist_index] if playlist_index >= 0 else None
        return {
            "vod_id": str(getattr(session.vod, "vod_id", "") or ""),
            "vod_name": str(getattr(session.vod, "vod_name", "") or ""),
            "episode_title": str(getattr(current_item, "title", "") or ""),
            "session_id": str(id(session)),
            "source_group_index": int(getattr(session, "source_group_index", -1) or -1),
            "source_index": int(getattr(session, "source_index", -1) or -1),
            "playlist_index": playlist_index,
            "url_summary": _summarize_media_url(str(getattr(current_item, "url", "") or "")),
        }

    def _write_structured_playback_log(self, message: str, *, level: str, category: str) -> None:
        if not self._logging_enabled() or self._app_log_service is None:
            return
        context = self._current_log_context()
        self._app_log_service.write_event(
            AppLogEvent(
                timestamp=datetime.now().isoformat(timespec="milliseconds"),
                level=level,
                source="player",
                category=category,
                message=message,
                module=__name__,
                vod_id=str(context["vod_id"]),
                vod_name=str(context["vod_name"]),
                episode_title=str(context["episode_title"]),
                session_id=str(context["session_id"]),
                url_summary=str(context["url_summary"]),
                source_group_index=int(context["source_group_index"]),
                source_index=int(context["source_index"]),
                playlist_index=int(context["playlist_index"]),
            )
        )

    def _append_log(
        self,
        message: str,
        *,
        dedupe: bool = False,
        level: str = "INFO",
        category: str = "playback",
    ) -> None:
        if not message:
            return
        if dedupe and self._last_log_message == message:
            return
        self._write_structured_playback_log(message, level=level, category=category)
        if not self._logging_enabled() and level not in {"WARNING", "ERROR", "CRITICAL"}:
            return
        formatted_message = self._format_log_line(message)
        existing_text = self.log_view.toPlainText()
        if existing_text:
            self.log_view.append(formatted_message)
        else:
            self.log_view.setPlainText(formatted_message)
        self._last_log_message = message

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
        self._set_startup_state(self._startup_coordinator.resolving(self._resolving_startup_message(current_item)))
        if wait_for_load:
            self._append_log(f"正在加载视频详情: {current_item.title}")
        self._play_item_request_id += 1
        request_id = self._play_item_request_id
        self._pending_play_item_load = _PendingPlayItemLoad(
            index=self.current_index,
            previous_index=previous_index,
            start_position_seconds=start_position_seconds,
            pause=pause,
            wait_for_load=wait_for_load,
            vod_snapshot=session.vod,
            intent_generation=self._playback_intent_generation,
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
        if self._should_skip_playback_prepare(current_item):
            return False
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
            intent_generation=self._playback_intent_generation,
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

    def _should_skip_playback_prepare(self, current_item: PlayItem) -> bool:
        resolved_url = (current_item.url or "").strip()
        if resolved_url.startswith(self._DASH_DATA_URI_PREFIX):
            return False
        if self._should_skip_live_m3u8_prepare(current_item, resolved_url):
            return True
        if self._is_ytdlp_resolved_direct_media_item(current_item, resolved_url):
            return True
        selected_quality_id = current_item.selected_playback_quality_id or ""
        if current_item.audio_url:
            return True
        if selected_quality_id.startswith("ytdlp_"):
            return True
        return any((quality.id or "").startswith("ytdlp_") for quality in current_item.playback_qualities)

    def _is_ytdlp_resolved_direct_media_item(self, current_item: PlayItem, resolved_url: str) -> bool:
        source_url = str(current_item.original_url or "").strip()
        if not source_url or not resolved_url:
            return False
        if not self._is_youtube_page_url(source_url):
            return False
        return not self._is_youtube_page_url(resolved_url)

    def _should_skip_live_m3u8_prepare(self, current_item: PlayItem, resolved_url: str) -> bool:
        if self.session is None or current_item.parse_required:
            return False
        if getattr(self.session.vod, "detail_style", "") != "live":
            return False
        return _is_backend_proxy_url(resolved_url)

    def _playback_prepare_source_url(self, current_item: PlayItem) -> str:
        preferred_url = (current_item.original_url or current_item.url).strip()
        resolved_url = current_item.url.strip()
        if resolved_url.startswith(self._DASH_DATA_URI_PREFIX):
            return resolved_url
        if self._is_youtube_page_url(preferred_url) and resolved_url and not self._is_youtube_page_url(resolved_url):
            return resolved_url
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
        self._sync_playlist_item_styles()
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
            self._sync_playlist_item_styles()
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
        should_apply_resolved_vod = True
        if (
            resolved_vod is not None
            and pending_load is not None
            and self.session is not None
            and self.current_index == pending_load.index
            and self.session.vod is not pending_load.vod_snapshot
        ):
            should_apply_resolved_vod = False
        if resolved_vod is not None and should_apply_resolved_vod:
            self._apply_resolved_vod(resolved_vod)
        elif resolved_vod is not None:
            self._update_original_metadata_snapshot(resolved_vod)
        if pending_load is None or not pending_load.wait_for_load:
            return
        if self.session is None or self.current_index != pending_load.index:
            return
        current_item = self.session.playlist[self.current_index]
        if not current_item.url:
            if self._try_auto_switch_source_after_failure():
                return
            self._show_failed_startup_state(f"播放失败: 没有可用的播放地址: {current_item.title}")
            self._mark_playback_stopped()
            self._restore_current_index(pending_load.previous_index)
            self._append_log(f"播放失败: 没有可用的播放地址: {current_item.title}")
            return
        try:
            resume_pause = self._resolve_pending_pause(
                pending_load.pause, pending_load.intent_generation
            )
            if self._start_playback_prepare(
                previous_index=pending_load.previous_index,
                start_position_seconds=pending_load.start_position_seconds,
                pause=resume_pause,
            ):
                return
            self._start_current_item_playback(
                start_position_seconds=pending_load.start_position_seconds,
                pause=resume_pause,
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
            if self._try_auto_switch_source_after_failure():
                return
            self._show_failed_startup_state(f"播放失败: {message}")
            self._mark_playback_stopped()
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
        previous_detail_poster_source = self._preferred_detail_poster_source()
        self._apply_playback_loader_result(load_result)
        self._render_playlist_items()
        if previous_detail_poster_source != self._preferred_detail_poster_source():
            self._reset_metadata_poster_index()
            self._render_poster()
        else:
            self._render_video_poster()
        self._render_metadata()
        self._render_detail_fields()
        self._refresh_window_title()
        self._refresh_parse_combo_enabled_state()
        logger.info(
            (
                "Playback loader succeeded request_id=%s hydrate_only=%s current_index=%s "
                "title=%s has_url=%s has_danmaku=%s pending_danmaku=%s"
            ),
            request_id,
            pending_loader.hydrate_only,
            self.current_index,
            self.session.playlist[self.current_index].title if self.session and 0 <= self.current_index < len(self.session.playlist) else "",
            bool(self.session and 0 <= self.current_index < len(self.session.playlist) and self.session.playlist[self.current_index].url),
            bool(self.session and 0 <= self.current_index < len(self.session.playlist) and self.session.playlist[self.current_index].danmaku_xml),
            bool(self.session and 0 <= self.current_index < len(self.session.playlist) and self.session.playlist[self.current_index].danmaku_pending),
        )
        if pending_loader.youtube_detail_parse:
            self._append_log("详情解析完成", dedupe=True)
        if pending_loader.hydrate_only:
            current_item = self.session.playlist[self.current_index]
            self._maybe_restore_cached_danmaku_for_current_item(allow_with_playback_loader=True)
            self._refresh_subtitle_state()
            self._schedule_followup_subtitle_refresh_if_needed(current_item)
            self._refresh_audio_state()
            self._refresh_video_quality_state()
            if not self._maybe_upgrade_ytdlp_playback_quality(current_item, pending_loader):
                pending_prepare = self._pending_playback_prepare
                if pending_prepare is None or pending_prepare.index != self.current_index:
                    self._configure_danmaku_for_current_item()
            return
        current_item = self.session.playlist[self.current_index]
        self._maybe_restore_cached_danmaku_for_current_item(allow_with_playback_loader=True)
        if self._should_delay_ytdlp_metadata_hydration(current_item):
            self._schedule_ytdlp_metadata_hydration(
                expected_item=current_item,
                previous_index=pending_loader.previous_index,
            )
        if not current_item.url:
            if self._try_auto_switch_source_after_failure():
                return
            self._show_failed_startup_state(f"播放失败: 没有可用的播放地址: {current_item.title}")
            self._mark_playback_stopped()
            self._restore_or_keep_current_index_after_failure(pending_loader.previous_index)
            self._append_log(f"播放失败: 没有可用的播放地址: {current_item.title}")
            return
        try:
            resume_pause = self._resolve_pending_pause(
                pending_loader.pause, pending_loader.intent_generation
            )
            if self._start_playback_prepare(
                previous_index=pending_loader.previous_index,
                start_position_seconds=pending_loader.start_position_seconds,
                pause=resume_pause,
            ):
                return
            self._start_current_item_playback(
                start_position_seconds=pending_loader.start_position_seconds,
                pause=resume_pause,
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
        if pending_loader.hydrate_only:
            self._append_log(f"详情加载失败: {message}")
            return
        if self._try_auto_switch_source_after_failure():
            return
        self._show_failed_startup_state(f"播放失败: {message}")
        self._mark_playback_stopped()
        self._restore_or_keep_current_index_after_failure(pending_loader.previous_index)
        self._append_log(f"播放失败: {message}")

    def _start_metadata_hydration(self) -> None:
        if self.session is None or self.session.metadata_hydrator is None or self.session.metadata_hydrated:
            return
        self._metadata_request_id += 1
        request_id = self._metadata_request_id
        session = self.session
        self._pending_metadata_session = session
        session.metadata_hydrated = True
        if self._restart_episode_title_after_next_metadata_hydration:
            self._force_episode_title_restart_on_metadata_request_id = request_id
            self._restart_episode_title_after_next_metadata_hydration = False
        override_title = self._metadata_hydration_override_title
        override_year = self._metadata_hydration_override_year
        override_category = self._metadata_hydration_override_category
        self._metadata_hydration_override_title = ""
        self._metadata_hydration_override_year = ""
        self._metadata_hydration_override_category = ""
        hydration_session = session
        if override_title or override_year or override_category:
            hydration_playlist = session.playlist
            if override_title and 0 <= session.start_index < len(session.playlist):
                hydration_playlist = list(session.playlist)
                hydration_playlist[session.start_index] = replace(
                    hydration_playlist[session.start_index],
                    media_title=override_title,
                )
            hydration_session = replace(
                session,
                vod=replace(
                    session.vod,
                    vod_name=override_title or session.vod.vod_name,
                    vod_year=override_year or session.vod.vod_year,
                    category_name=override_category or session.vod.category_name,
                ),
                playlist=hydration_playlist,
            )
        hydration_session = replace(
            hydration_session,
            vod=self._metadata_hydration_vod(hydration_session.vod),
        )
        hydration_current_item = None
        if 0 <= hydration_session.start_index < len(hydration_session.playlist):
            hydration_current_item = hydration_session.playlist[hydration_session.start_index]
        hydration_query = MetadataContext(
            vod=hydration_session.vod,
            source_kind=str(getattr(hydration_session, "source_kind", "") or ""),
            source_key=str(getattr(hydration_session, "source_key", "") or ""),
            current_item=hydration_current_item,
        ).to_query()
        self._append_log(_build_metadata_hydration_query_log(hydration_query), dedupe=True)

        def run() -> None:
            try:
                updated_vod = session.metadata_hydrator(hydration_session)
            except Exception as exc:
                if self._is_window_alive():
                    self._metadata_hydration_signals.failed.emit(request_id, str(exc))
                return
            if not self._is_window_alive():
                return
            self._metadata_hydration_signals.succeeded.emit(request_id, updated_vod)

        threading.Thread(target=run, daemon=True).start()

    def _rerun_episode_title_enhancement(self) -> None:
        """手动触发一次剧集标题增强（右键菜单"重写剧集标题"）。

        忽略上次保存的标题缓存，强制按当前（可能已被刮削/重定向修正过的）标题重新
        搜索官方分集标题。"""
        if self.session is None:
            return
        if self.session.episode_title_enhancer is None:
            self._append_log("当前来源不支持剧集标题增强")
            return
        self._episode_title_request_id += 1
        self.session.episode_titles_hydrated = False
        self.session.episode_titles_force_refresh = True
        self._append_log("重新搜索分集标题...")
        self._start_episode_title_enhancement()

    def _start_episode_title_enhancement(self) -> None:
        if self.session is None or self.session.episode_title_enhancer is None or self.session.episode_titles_hydrated:
            return
        if (
            self.session.async_playback_loader
            and self._pending_playback_loader is not None
            and 0 <= self.current_index < len(self.session.playlist)
            and not str(self.session.playlist[self.current_index].url or "").strip()
        ):
            logger.info(
                "Delay episode title enhancement until playback loader resolves index=%s title=%s",
                self.current_index,
                self.session.playlist[self.current_index].title,
            )
            return
        self._episode_title_request_id += 1
        request_id = self._episode_title_request_id
        session = self.session
        self._pending_episode_title_session = session
        session.episode_titles_hydrated = True

        def run() -> None:
            try:
                updated_playlist = session.episode_title_enhancer(session)
            except Exception as exc:
                if self._is_window_alive():
                    self._episode_title_enhancement_signals.failed.emit(request_id, str(exc))
                return
            if not self._is_window_alive():
                return
            self._episode_title_enhancement_signals.succeeded.emit(request_id, updated_playlist)

        threading.Thread(target=run, daemon=True).start()

    def _handle_metadata_hydration_succeeded(self, request_id: int, updated_vod: VodItem | None) -> None:
        if request_id != self._metadata_request_id:
            return
        pending_session = self._pending_metadata_session
        self._pending_metadata_session = None
        force_restart_episode_titles = request_id == self._force_episode_title_restart_on_metadata_request_id
        if force_restart_episode_titles:
            self._force_episode_title_restart_on_metadata_request_id = 0
        if updated_vod is None or pending_session is None:
            return
        if self.session is not pending_session:
            return
        previous_vod = self.session.vod
        metadata_log = _build_metadata_update_log(previous_vod, updated_vod)
        self.session.vod = updated_vod
        self._sync_playlist_media_title_from_metadata(previous_vod, updated_vod)
        research_item = self._current_play_item()
        if (
            research_item is not None
            and not research_item.danmaku_xml
            and not research_item.danmaku_pending
        ):
            self._danmaku_research_pending = True
        self._reset_metadata_poster_index()
        self._render_poster()
        self._render_metadata()
        self._render_detail_fields()
        self._refresh_metadata_original_toggle()
        self._refresh_window_title()
        if metadata_log:
            self._append_log(metadata_log)
        if force_restart_episode_titles:
            self.session.episode_titles_hydrated = False
            self._start_episode_title_enhancement()
        else:
            self._maybe_research_danmaku_after_metadata()

    def _maybe_research_danmaku_after_metadata(self) -> None:
        """Re-search danmaku once after metadata hydrates the current item.

        Metadata can improve the query (corrected title, variety genre -> variety
        issue label) and supply an official platform URL (provider pin). If the
        first search (often issued before metadata arrived) left the current item
        without danmaku, re-search now. The guard key combines query and provider
        URL so a pin-only change still re-searches, while an unchanged signal does
        not loop.
        """
        if not self._danmaku_research_pending:
            return
        self._danmaku_research_pending = False
        if self.session is None:
            return
        controller = getattr(self.session, "danmaku_controller", None)
        current_item = self._current_play_item()
        if controller is None or current_item is None:
            return
        if current_item.danmaku_xml or current_item.danmaku_pending:
            return
        query = str(current_item.danmaku_search_query or "").strip()
        if not query:
            return
        research_key = f"{query}\x1f{str(current_item.metadata_provider_url or '').strip()}"
        if research_key == self._danmaku_last_searched_query:
            return
        self._danmaku_last_searched_query = research_key
        auto_resolve = getattr(controller, "auto_resolve_danmaku", None)
        if not callable(auto_resolve):
            return
        self._start_danmaku_source_task(
            current_item,
            error_prefix="弹幕自动下载失败",
            task=lambda: auto_resolve(
                current_item,
                playlist=self.session.playlist,
                media_duration_seconds=self._current_media_duration_seconds(),
            ),
            configure_danmaku_on_success=True,
            debug_label="元数据后重搜",
        )

    def _sync_playlist_media_title_from_metadata(self, previous_vod: VodItem, updated_vod: VodItem) -> None:
        if self.session is None:
            return
        metadata_provider_url = extract_official_link_url(updated_vod.detail_fields)
        if metadata_provider_url:
            for item in self.session.playlist:
                if not str(item.metadata_provider_url or "").strip():
                    item.metadata_provider_url = metadata_provider_url
        corrected_title = str(updated_vod.vod_name or "").strip()
        if not corrected_title:
            return
        previous_title = str(previous_vod.vod_name or "").strip()
        stale_titles = {title for title in (previous_title,) if title}
        current_item = self._current_play_item()
        if current_item is not None:
            current_media_title = str(current_item.media_title or "").strip()
            if current_media_title:
                stale_titles.add(current_media_title)
        for item in self.session.playlist:
            media_title = str(item.media_title or "").strip()
            if not media_title or media_title in stale_titles:
                if not self._should_preserve_season_marked_title(media_title, corrected_title):
                    item.media_title = corrected_title
            danmaku_title = str(item.danmaku_search_title or "").strip()
            if item.danmaku_search_query_overridden:
                continue
            if not danmaku_title or danmaku_title in stale_titles:
                if self._should_preserve_season_marked_title(danmaku_title, corrected_title):
                    continue
                item.danmaku_search_title = corrected_title
                item.danmaku_search_query = " ".join(
                    part
                    for part in (
                        corrected_title,
                        str(item.danmaku_search_episode or "").strip(),
                    )
                    if part
                ).strip()

    def _should_preserve_season_marked_title(self, current_title: str, corrected_title: str) -> bool:
        current_text = str(current_title or "").strip()
        corrected_text = str(corrected_title or "").strip()
        if not current_text or not corrected_text:
            return False
        stripped = strip_match_season_suffix(current_text)
        if stripped == current_text:
            return False
        return normalize_match_title(stripped) == normalize_match_title(corrected_text)

    def _should_restart_episode_title_enhancement(self, previous_vod: VodItem, updated_vod: VodItem) -> bool:
        if self.session is None or self.session.episode_title_enhancer is None:
            return False
        previous_signature = (
            str(previous_vod.vod_name or "").strip(),
            str(previous_vod.vod_year or "").strip(),
            str(previous_vod.type_name or "").strip(),
            str(previous_vod.category_name or "").strip(),
        )
        updated_signature = (
            str(updated_vod.vod_name or "").strip(),
            str(updated_vod.vod_year or "").strip(),
            str(updated_vod.type_name or "").strip(),
            str(updated_vod.category_name or "").strip(),
        )
        return previous_signature != updated_signature

    @staticmethod
    def _playlist_identity_key(item: PlayItem) -> tuple[str, str, str, str, str]:
        return (
            item.vod_id.strip(),
            item.original_title.strip(),
            item.path.strip(),
            item.title.strip(),
            item.play_source.strip(),
        )

    def _find_updated_playlist_index(
        self,
        updated_playlist: list[PlayItem],
        current_item: PlayItem | None,
        fallback_index: int,
    ) -> int:
        if not updated_playlist:
            return 0
        if current_item is None:
            return max(0, min(fallback_index, len(updated_playlist) - 1))
        identity = self._playlist_identity_key(current_item)
        for index, candidate in enumerate(updated_playlist):
            if self._playlist_identity_key(candidate) == identity:
                return index
        return max(0, min(fallback_index, len(updated_playlist) - 1))

    def _merge_episode_title_enhancement_item(self, existing_item: PlayItem, updated_item: PlayItem) -> PlayItem:
        existing_item.title = updated_item.title
        existing_item.original_title = updated_item.original_title
        existing_item.episode_display_title = updated_item.episode_display_title
        existing_item.episode_title_source = updated_item.episode_title_source
        if updated_item.media_title:
            existing_item.media_title = updated_item.media_title
        if updated_item.type_name:
            existing_item.type_name = updated_item.type_name
        if updated_item.category_name:
            existing_item.category_name = updated_item.category_name
        return existing_item

    def _merge_episode_title_enhancement_playlist(
        self,
        updated_playlist: list[PlayItem],
    ) -> list[PlayItem]:
        if self.session is None:
            return list(updated_playlist)
        existing_items_by_identity = {
            self._playlist_identity_key(item): item
            for item in self.session.playlist
        }
        merged_playlist: list[PlayItem] = []
        for updated_item in updated_playlist:
            identity = self._playlist_identity_key(updated_item)
            existing_item = existing_items_by_identity.get(identity)
            if existing_item is None:
                merged_playlist.append(updated_item)
                continue
            merged_playlist.append(self._merge_episode_title_enhancement_item(existing_item, updated_item))
        return merged_playlist

    def _handle_episode_title_enhancement_succeeded(self, request_id: int, updated_playlist: list[PlayItem] | None) -> None:
        if request_id != self._episode_title_request_id:
            return
        pending_session = self._pending_episode_title_session
        self._pending_episode_title_session = None
        if updated_playlist is None or pending_session is None:
            return
        if self.session is not pending_session:
            return
        current_item = self.session.playlist[self.current_index] if 0 <= self.current_index < len(self.session.playlist) else None
        merged_playlist = self._merge_episode_title_enhancement_playlist(updated_playlist)
        self._playlist_sort_state.remember(merged_playlist)
        self.session.playlist = merged_playlist
        self._playlist_sort_state.apply(self.session.playlist)
        self._apply_episode_title_overrides_to_session()
        self.current_index = find_playlist_item_index(
            self.session.playlist,
            current_item,
            self.current_index,
        )
        self.session.start_index = self.current_index
        if 0 <= self.session.playlist_index < len(self.session.playlists):
            self.session.playlists[self.session.playlist_index] = self.session.playlist
        source_groups = self._session_source_groups()
        if 0 <= self.session.source_group_index < len(source_groups):
            group = source_groups[self.session.source_group_index]
            if 0 <= self.session.source_index < len(group.sources):
                group.sources[self.session.source_index].playlist = self.session.playlist
        self.playlist_title_mode = "episode"
        self._render_playlist_sort_combo()
        self._render_playlist_title_tabs()
        self._render_playlist_items()
        self._refresh_window_title()
        self._log_episode_title_mapping()
        self._maybe_research_danmaku_after_metadata()

    def _log_episode_title_mapping(self) -> None:
        if self.session is None:
            return
        playlist = self.session.playlist
        lines: list[str] = []
        has_mapping = False
        for item in playlist:
            original = str(item.original_title or "").strip()
            display = str(item.episode_display_title or "").strip()
            if display and normalize_episode_title_text(original) != normalize_episode_title_text(display):
                source = str(item.episode_title_source or "").strip()
                source_suffix = f" [来源: {source}]" if source else ""
                lines.append(f"{original} → {display}{source_suffix}")
                has_mapping = True
            else:
                lines.append(original)
        if not has_mapping:
            return
        logger.info("剧集标题改写:\n%s", "\n".join(f"  {line}" for line in lines))

    def _handle_metadata_hydration_failed(self, request_id: int, message: str) -> None:
        if request_id != self._metadata_request_id:
            return
        self._pending_metadata_session = None
        if request_id == self._force_episode_title_restart_on_metadata_request_id:
            self._force_episode_title_restart_on_metadata_request_id = 0
        self._append_log(f"元数据补全失败: {message}")

    def _handle_episode_title_enhancement_failed(self, request_id: int, message: str) -> None:
        if request_id != self._episode_title_request_id:
            return
        self._pending_episode_title_session = None
        self._append_log(f"剧集标题增强失败: {message}")

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
        if not self._should_preserve_original_url_after_prepare(current_item, pending_prepare.source_url):
            current_item.original_url = pending_prepare.source_url
        if pending_prepare.requested_dash_video_id:
            current_item.dash_video_id = pending_prepare.requested_dash_video_id
        current_item.url = prepared_url
        self._maybe_restore_cached_danmaku_for_current_item()
        self._refresh_video_quality_state(prepared_url)
        try:
            self._start_current_item_playback(
                start_position_seconds=pending_prepare.start_position_seconds,
                pause=self._resolve_pending_pause(
                    pending_prepare.pause, pending_prepare.intent_generation
                ),
            )
        except Exception as exc:
            self._restore_current_index(pending_prepare.previous_index)
            self._append_log(f"播放失败: {exc}")

    def _should_preserve_original_url_after_prepare(self, current_item: PlayItem, source_url: str) -> bool:
        if not source_url.startswith(self._DASH_DATA_URI_PREFIX):
            return False
        selected_quality_id = current_item.selected_playback_quality_id or ""
        return bool(current_item.original_url) and selected_quality_id.startswith("ytdlp_")

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
            if self._try_auto_switch_source_after_failure():
                return
            self._show_failed_startup_state(f"播放失败: {message}")
            self._mark_playback_stopped()
            self._append_log(f"播放失败: {message}")
            self._restore_current_index(pending_prepare.previous_index)
            return
        self._show_failed_startup_state(f"播放失败: {message}")
        current_item.dash_video_id = pending_prepare.previous_dash_video_id
        self._refresh_video_quality_state(current_item.url)
        self._append_log(f"播放代理失败，继续播放原地址: {message}")
        try:
            self._start_current_item_playback(
                start_position_seconds=pending_prepare.start_position_seconds,
                pause=self._resolve_pending_pause(
                    pending_prepare.pause, pending_prepare.intent_generation
                ),
            )
        except Exception as exc:
            self._restore_current_index(pending_prepare.previous_index)
            self._append_log(f"播放失败: {exc}")

    def _current_item_load_is_pending(self) -> bool:
        if self.session is None:
            return False
        pending_playback_loader = self._pending_playback_loader
        if (
            pending_playback_loader is not None
            and getattr(pending_playback_loader, "hydrate_only", False)
        ):
            pending_playback_loader = None
        pending_items = (
            self._pending_play_item_load,
            pending_playback_loader,
            self._pending_playback_prepare,
        )
        return any(pending is not None and pending.index == self.current_index for pending in pending_items)

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
            duration_seconds = self._current_media_duration_seconds()
            item = self._current_play_item()

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
                    duration_seconds=duration_seconds,
                )

            self._enqueue_controller_task("进度上报失败", report)
            if item is not None:
                self._following_progress_reporter(
                    item,
                    position_seconds=int(position_seconds),
                    duration_seconds=int(duration_seconds),
                )
                self._record_heat_effective_watch_if_needed(
                    item,
                    position_seconds=int(position_seconds),
                    duration_seconds=int(duration_seconds),
                )
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

    def _toggle_log_visibility(self) -> None:
        if self.config is not None and getattr(self.config, "player_log_visible", True) != self.toggle_log_button.isChecked():
            self.config.player_log_visible = self.toggle_log_button.isChecked()
            self._save_config()
        self._apply_visibility_state()

    def _reanchor_metadata_scrape_binding_for_drive_group(
        self,
        group,
        playlist: list,
    ) -> None:
        # 网盘合集里每个目录是一部剧；切到某目录后再刮削时，绑定应挂在"重新打开该
        # 目录时用于查询的标题"（目录清洗名）上，而不是打开合集时的分享/卡片标题，
        # 否则重开后按目录标题查询会 miss。已加载分组的 media_title 若被元数据改写
        # （等于当前 vod_name），保持原锚点，避免把绑定挂到刮削后的标题上。
        session = self.session
        if session is None or not str(getattr(session, "drive_resource_id", "") or "").strip():
            return
        title = ""
        if str(getattr(group, "drive_dir_id", "") or "").strip():
            title = clean_drive_directory_title(getattr(group, "label", ""))
        elif playlist:
            media_title = str(playlist[0].media_title or "").strip()
            vod_name = str(session.vod.vod_name or "").strip()
            if media_title and media_title != vod_name:
                title = media_title
        title = title.strip()
        current_anchor = self._metadata_scrape_binding_title
        if not title or normalize_metadata_binding_title(title) == normalize_metadata_binding_title(current_anchor):
            return
        self._metadata_scrape_binding_title = title

    def _switch_active_source(
        self,
        source_group_index: int,
        source_index: int,
        *,
        reset_auto_switch_state: bool = True,
        target_index: int | None = None,
    ) -> None:
        if self.session is None:
            return
        source_groups = self._session_source_groups()
        if not (0 <= source_group_index < len(source_groups)):
            return
        active_group = source_groups[source_group_index]
        if not (0 <= source_index < len(active_group.sources)):
            return
        if (
            source_group_index == self.session.source_group_index
            and source_index == self.session.source_index
        ):
            return
        target_playlist = active_group.sources[source_index].playlist
        active_source = active_group.sources[source_index]
        if active_source.subgroups:
            active_source.subgroup_index = max(0, min(active_source.subgroup_index, len(active_source.subgroups) - 1))
            self._ensure_drive_subgroup_loaded(active_source.subgroups[active_source.subgroup_index])
            if active_source.subgroups[active_source.subgroup_index].sources:
                target_playlist = active_source.subgroups[active_source.subgroup_index].sources[0].playlist
        if not target_playlist:
            self.session.source_group_index = source_group_index
            self.session.source_index = source_index
            self._render_playlist_source_combos()
            self._render_playlist_items()
            self._render_bilibili_playlist_tree()
            self._sync_playlist_panel_mode()
            return
        previous_index = self.current_index
        if target_index is None:
            target_index = min(previous_index, len(target_playlist) - 1)
        else:
            target_index = max(0, min(target_index, len(target_playlist) - 1))
        _, mapping = self._flatten_source_groups(source_groups)
        self.report_progress(force_remote_report=True)
        self._stop_current_playback()
        self._invalidate_play_item_resolution()
        if reset_auto_switch_state:
            self._reset_auto_switched_failure_sources()
        self.session.source_group_index = source_group_index
        self.session.source_index = source_index
        self.session.playlist_index = mapping[(source_group_index, source_index)]
        self.session.playlist = target_playlist
        self._reanchor_metadata_scrape_binding_for_drive_group(
            active_group,
            target_playlist,
        )
        self._playlist_sort_state.apply(target_playlist)
        reset_prefetch = getattr(self.controller, "reset_next_episode_danmaku_prefetch_state", None)
        if callable(reset_prefetch):
            reset_prefetch(self.session)
        self.current_index = target_index
        self.session.start_index = self.current_index
        self.playlist_title_mode = "episode"
        self._render_playlist_source_combos()
        self._render_playlist_sort_combo()
        self._render_playlist_title_tabs()
        self._render_playlist_items()
        self._render_bilibili_playlist_tree()
        self._sync_playlist_panel_mode()
        self.session.episode_titles_hydrated = False
        self._start_episode_title_enhancement()
        try:
            self._load_current_item(previous_index=previous_index)
            self._refresh_window_title()
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")

    def _change_playlist_group(self, group_index: int) -> None:
        self._ensure_drive_group_loaded(group_index)
        self._switch_active_source(group_index, 0)

    def _ensure_drive_group_loaded(self, group_index: int) -> None:
        # Lazily fetch a drive directory's files the first time it is selected. Runs on the UI
        # thread (local backend, one directory is small); failures degrade to an empty list.
        session = self.session
        if session is None or session.drive_files_loader is None or not session.drive_resource_id:
            return
        groups = self._session_source_groups()
        if not (0 <= group_index < len(groups)):
            return
        group = groups[group_index]
        if not group.drive_dir_id or (group.sources and group.sources[0].playlist):
            return
        # Each directory in a share collection is its own drama, so derive the media title
        # from the directory name (not the collection's vod_name) — matching the eager-load
        # path in build_drive_grouped_sources.
        media_title = clean_drive_directory_title(group.label)
        try:
            videos = session.drive_files_loader(session.drive_resource_id, group.drive_dir_id) or []
        except Exception as exc:
            logger.warning("drive directory load failed dir=%s", group.drive_dir_id, exc_info=exc)
            self._append_log(f"加载目录失败: {exc}")
            return
        new_items = [
            map_drive_video_to_play_item(video, index=i, media_title=media_title, play_source=group.label)
            for i, video in enumerate(videos)
            if video.get("url")
        ]
        # Mutate the existing playlist in place so session.playlist / session.playlists
        # (which reference the same list object) stay in sync without a re-switch.
        if group.sources and group.sources[0].playlist is not None:
            target = group.sources[0].playlist
            target.clear()
            target.extend(new_items)
        elif group.sources:
            group.sources[0].playlist = list(new_items)
        else:
            group.sources = [PlaybackSource(label=group.label, playlist=list(new_items))]

    def _change_playlist_source(self, source_index: int) -> None:
        if self.session is None:
            return
        self._switch_active_source(self.session.source_group_index, source_index)

    def _change_playlist_subgroup(self, subgroup_index: int) -> None:
        if self.session is None:
            return
        groups = self._session_source_groups()
        if not (0 <= self.session.source_group_index < len(groups)):
            return
        group = groups[self.session.source_group_index]
        if not (0 <= self.session.source_index < len(group.sources)):
            return
        source = group.sources[self.session.source_index]
        if not (0 <= subgroup_index < len(source.subgroups)):
            return
        source.subgroup_index = subgroup_index
        subgroup = source.subgroups[subgroup_index]
        self._ensure_drive_subgroup_loaded(subgroup)
        if subgroup.sources:
            # Subgroups are represented as a single leaf source; reuse the existing
            # switching path while retaining the parent group/source indexes.
            target = subgroup.sources[0].playlist
            if target:
                self._switch_active_playlist(target, subgroup.label)
                self._reanchor_metadata_scrape_binding_for_drive_group(subgroup, target)
        self._render_playlist_source_combos()

    def _ensure_drive_subgroup_loaded(self, subgroup: PlaybackSourceGroup) -> None:
        session = self.session
        if session is None:
            return
        groups = self._session_source_groups()
        active_source = None
        if 0 <= session.source_group_index < len(groups):
            group = groups[session.source_group_index]
            if 0 <= session.source_index < len(group.sources):
                active_source = group.sources[session.source_index]
        files_loader = active_source.drive_files_loader if active_source is not None else None
        resource_id = active_source.drive_resource_id if active_source is not None else ""
        files_loader = files_loader or session.drive_files_loader
        resource_id = resource_id or session.drive_resource_id
        if (
            files_loader is None
            or not resource_id
            or not subgroup.drive_dir_id
            or (subgroup.sources and subgroup.sources[0].playlist)
        ):
            return
        try:
            videos = files_loader(resource_id, subgroup.drive_dir_id) or []
        except Exception as exc:
            logger.warning("drive directory load failed dir=%s", subgroup.drive_dir_id, exc_info=exc)
            self._append_log(f"加载目录失败: {exc}")
            return
        playlist = [
            map_drive_video_to_play_item(
                video,
                index=index,
                media_title=clean_drive_directory_title(subgroup.label),
                play_source=subgroup.label,
            )
            for index, video in enumerate(videos)
            if video.get("url")
        ]
        if subgroup.sources:
            subgroup.sources[0].playlist[:] = playlist
        else:
            subgroup.sources = [PlaybackSource(label=subgroup.label, playlist=playlist)]

    def _switch_active_playlist(
        self,
        target_playlist: list[PlayItem],
        label: str = "",
        *,
        target_index: int | None = None,
    ) -> None:
        if self.session is None or not target_playlist:
            return
        previous_index = self.current_index
        self.report_progress(force_remote_report=True)
        self._stop_current_playback()
        self.session.playlist = target_playlist
        if target_index is None:
            target_index = min(previous_index, len(target_playlist) - 1)
        self.current_index = max(0, min(target_index, len(target_playlist) - 1))
        self.session.start_index = self.current_index
        self._playlist_sort_state.apply(target_playlist)
        self._render_playlist_source_combos()
        self._render_playlist_sort_combo()
        self._render_playlist_title_tabs()
        self._render_playlist_items()
        self._sync_playlist_panel_mode()
        try:
            self._load_current_item(previous_index=previous_index)
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
            self.controls.seek_relative(seconds)
            self._mark_recent_user_seek(None)
        except Exception as exc:
            self._append_log(f"跳转失败: {exc}")

    def _replay_current_item(self) -> None:
        if self.session is None:
            return
        self.report_progress(force_remote_report=True)
        self._stop_current_playback()
        self.is_playing = True
        self._sync_native_always_on_top(failure_message="播放时置顶同步失败")
        self._update_play_button_icon()
        self._refresh_window_title()
        self.playlist.setCurrentRow(self.current_index)
        self._load_current_item(
            start_position_seconds=0,
            preserve_primary_external_subtitle_selection=True,
        )

    def _toggle_mute(self) -> None:
        try:
            self.controls.toggle_mute()
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
            self.controls.set_muted(self._is_muted)
        except Exception as exc:
            self._append_log(f"静音恢复失败: {exc}")

    def _change_speed(self, text: str) -> None:
        try:
            self.current_speed = float(text.rstrip("x"))
            self.controls.set_speed(self.current_speed)
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
        self._refresh_width_adaptive_control_visibility()

    def _reset_danmaku_combo(self, *, enabled: bool = False, current_index: int = 0) -> None:
        self.danmaku_combo.blockSignals(True)
        self.danmaku_combo.clear()
        labels = ["弹幕", "关闭", *(f"{line_count}行" for line_count in range(1, 11))]
        for label in labels:
            self.danmaku_combo.addItem(label)
        self.danmaku_combo.setCurrentIndex(current_index)
        self.danmaku_combo.setEnabled(enabled)
        self.danmaku_combo.blockSignals(False)
        self._refresh_width_adaptive_control_visibility()

    def _reset_video_quality_combo(self) -> None:
        self.video_quality_combo.blockSignals(True)
        self.video_quality_combo.clear()
        self.video_quality_combo.addItem("清晰度", None)
        self.video_quality_combo.setCurrentIndex(0)
        self.video_quality_combo.setEnabled(False)
        self.video_quality_combo.blockSignals(False)
        self._refresh_width_adaptive_control_visibility()

    def _reset_audio_combo(self) -> None:
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        self.audio_combo.addItem("音轨", ("auto", None))
        self.audio_combo.setCurrentIndex(0)
        self.audio_combo.setEnabled(False)
        self.audio_combo.blockSignals(False)
        self._refresh_width_adaptive_control_visibility()

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
        self._refresh_width_adaptive_control_visibility()

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
        try:
            value = int(getattr(self.config, "preferred_danmaku_line_count", 1))
        except (TypeError, ValueError):
            return 1
        return max(1, min(value, 10))

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

    def _preferred_danmaku_opacity(self) -> int:
        if self.config is None:
            return 85
        try:
            value = int(getattr(self.config, "preferred_danmaku_opacity", 85))
        except (TypeError, ValueError):
            return 85
        clamped = max(30, min(value, 100))
        return max(30, min(int(round(clamped / 5) * 5), 100))

    def _preferred_danmaku_outline_strength(self) -> str:
        if self.config is None:
            return "strong"
        value = str(getattr(self.config, "preferred_danmaku_outline_strength", "strong") or "").strip()
        return value if value in {"off", "soft", "strong"} else "strong"

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

    def _apply_danmaku_keypad_line_count(self, line_count: int) -> None:
        if self.config is None:
            return
        normalized = max(0, min(int(line_count), 9))
        enabled = normalized > 0
        if (
            self.config.preferred_danmaku_enabled != enabled
            or self.config.preferred_danmaku_line_count != max(normalized, 1)
        ):
            self.config.preferred_danmaku_enabled = enabled
            if enabled:
                self.config.preferred_danmaku_line_count = normalized
            self._save_config()
        self._refresh_danmaku_combo_from_preferences()
        self._refresh_danmaku_settings_dialog_controls()
        if not self._current_play_item_danmaku_xml():
            return
        if not enabled:
            self._clear_active_danmaku()
            return
        try:
            self._enable_danmaku(normalized)
        except Exception as exc:
            self._append_log(f"弹幕切换失败: {exc}")
            self._clear_active_danmaku()
            self._reset_danmaku_combo(enabled=True, current_index=1)

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

    def _save_danmaku_opacity(self, value: int) -> None:
        if self.config is None:
            return
        clamped = max(30, min(int(value), 100))
        normalized = max(30, min(int(round(clamped / 5) * 5), 100))
        if int(getattr(self.config, "preferred_danmaku_opacity", 85)) == normalized:
            return
        self.config.preferred_danmaku_opacity = normalized
        self._save_config()
        self._refresh_danmaku_settings_dialog_controls()
        self._reload_active_danmaku_for_render_settings()

    def _save_danmaku_outline_strength(self, value: str) -> None:
        if self.config is None:
            return
        normalized = value if value in {"off", "soft", "strong"} else "strong"
        if str(getattr(self.config, "preferred_danmaku_outline_strength", "strong") or "").strip() == normalized:
            return
        self.config.preferred_danmaku_outline_strength = normalized
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
        self._refresh_width_adaptive_control_visibility()

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

    def _current_item_ytdlp_audio_tracks(self) -> list[YtdlpAudioTrackOption]:
        current_item = self._current_play_item()
        if current_item is None:
            return []
        return list(getattr(current_item, "audio_tracks", []) or [])

    def _current_item_has_ytdlp_audio_tracks(self) -> bool:
        return bool(self._current_item_ytdlp_audio_tracks())

    def _selected_ytdlp_audio_combo_index(self) -> int:
        current_item = self._current_play_item()
        if current_item is None:
            return 0
        selected_audio_track_id = str(getattr(current_item, "selected_audio_track_id", "") or "").strip()
        if not selected_audio_track_id:
            return 0
        for index in range(1, self.audio_combo.count()):
            item_data = self.audio_combo.itemData(index)
            if item_data == ("ytdlp", selected_audio_track_id):
                return index
        return 0

    def _current_item_secondary_external_subtitles(self) -> list[ExternalSubtitleOption]:
        return [subtitle for subtitle in self._current_item_external_subtitles() if subtitle.source != "spider"]

    def _current_item_auto_spider_external_subtitles(self) -> list[ExternalSubtitleOption]:
        return [subtitle for subtitle in self._current_item_external_subtitles() if subtitle.source == "spider"]

    def _configured_youtube_default_subtitle_lang(self) -> str:
        value = str(getattr(self.config, "youtube_default_subtitle_lang", "") or "").strip()
        return value if value in {"zh-CN", "zh-TW", "zh-HK", "en"} else ""

    def _matches_configured_youtube_default_subtitle(self, subtitle: ExternalSubtitleOption) -> bool:
        preferred_lang = self._configured_youtube_default_subtitle_lang()
        lang = str(subtitle.lang or "").strip()
        aliases = {
            "zh-CN": {"zh-CN", "zh-Hans", "zh"},
            "zh-TW": {"zh-TW", "zh-Hant"},
            "zh-HK": {"zh-HK", "zh-Hant"},
            "en": {"en"},
        }
        return bool(preferred_lang and lang in aliases.get(preferred_lang, {preferred_lang}))

    def _current_item_default_ytdlp_external_subtitle(self) -> ExternalSubtitleOption | None:
        return next(
            (
                subtitle
                for subtitle in self._current_item_external_subtitles()
                if subtitle.source == "ytdlp" and self._matches_configured_youtube_default_subtitle(subtitle)
            ),
            None,
        )

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
        return self._should_reload_primary_external_subtitle_for_preference(current_external_subtitle)

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
            return self._should_reload_primary_external_subtitle_for_preference(current_external_subtitle)
        return self._should_auto_apply_spider_subtitle()

    def _should_reload_primary_external_subtitle_for_preference(self, subtitle: ExternalSubtitleOption) -> bool:
        if self._subtitle_preference.mode == "external":
            return True
        if self._subtitle_preference.mode != "auto":
            return False
        if subtitle.source == "spider":
            return True
        return subtitle.source == "ytdlp" and self._matches_configured_youtube_default_subtitle(subtitle)

    def _remove_external_subtitle_track(self, track_id: int | None) -> None:
        if track_id is None or not hasattr(self.video, "remove_subtitle_track"):
            return
        self.video.remove_subtitle_track(track_id)

    def _clear_primary_external_subtitle(self, *, preserve_selection: bool = False) -> None:
        self._primary_external_subtitle_fetch = None
        self._stop_primary_external_subtitle_retry()
        self._remove_external_subtitle_track(self._primary_external_subtitle_track_id)
        if not preserve_selection:
            self._primary_external_subtitle_selection = None
        self._primary_external_subtitle_track_id = None
        self._primary_external_subtitle_path = None

    def _clear_secondary_external_subtitle(self, *, preserve_selection: bool = False) -> None:
        self._secondary_external_subtitle_fetch = None
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
        if not self._should_reload_primary_external_subtitle_for_preference(current_external_subtitle):
            return False
        if not self._ensure_primary_external_subtitle_loaded(current_external_subtitle):
            return True
        if not self._apply_primary_external_subtitle_track(self._primary_external_subtitle_track_id):
            return True
        self._sync_subtitle_combo_without_tracks()
        return True

    def _primary_external_subtitle_track_is_stale(self) -> bool:
        current_external_subtitle = self._current_primary_external_subtitle()
        track_id = self._primary_external_subtitle_track_id
        if current_external_subtitle is None or track_id is None:
            return False
        has_track = getattr(self.video, "has_subtitle_track", None)
        if not callable(has_track):
            return False
        try:
            return not bool(has_track(track_id))
        except Exception:
            return False

    def _invalidate_primary_external_subtitle_track(self) -> None:
        self._primary_external_subtitle_track_id = None
        self._primary_external_subtitle_path = None

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

    def _ensure_primary_external_subtitle_loaded(
        self,
        subtitle: ExternalSubtitleOption,
        *,
        purpose: str = "primary-auto",
    ) -> bool:
        if self._primary_external_subtitle_track_id is not None:
            return True
        if self._primary_external_subtitle_fetch is not None:
            return False
        self._start_external_subtitle_fetch(subtitle, secondary=False, purpose=purpose)
        return False

    def _apply_primary_external_subtitle_track(self, track_id: int | None) -> bool:
        if track_id is None:
            self._schedule_primary_external_subtitle_retry_for_pending_track()
            return False
        current_track_getter = getattr(self.video, "current_subtitle_track_id", None)
        if callable(current_track_getter):
            try:
                if current_track_getter() == track_id:
                    self._stop_primary_external_subtitle_retry()
                    return True
            except Exception:
                pass
        try:
            self.video.apply_subtitle_mode("track", track_id=track_id)
        except Exception as exc:
            if callable(current_track_getter):
                try:
                    if current_track_getter() == track_id:
                        self._stop_primary_external_subtitle_retry()
                        return True
                except Exception:
                    pass
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
            if not self._ensure_primary_external_subtitle_loaded(current_external_subtitle, purpose="primary-retry"):
                return
            track_id = self._primary_external_subtitle_track_id
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

    def _auto_apply_ytdlp_default_subtitle_if_needed(self) -> bool:
        if self._subtitle_preference.mode != "auto":
            return False
        if self._current_primary_external_subtitle() is not None:
            return False
        subtitle = self._current_item_default_ytdlp_external_subtitle()
        if subtitle is None:
            return False
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
        self._refresh_width_adaptive_control_visibility()

    def _populate_audio_combo(self, tracks: list[AudioTrack]) -> None:
        ytdlp_tracks = self._current_item_ytdlp_audio_tracks()
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        self.audio_combo.addItem("音轨", ("auto", None))
        if ytdlp_tracks:
            selected_index = 0
            current_item = self._current_play_item()
            selected_audio_track_id = "" if current_item is None else str(current_item.selected_audio_track_id or "").strip()
            for index, track in enumerate(ytdlp_tracks, start=1):
                self.audio_combo.addItem(track.label, ("ytdlp", track.id))
                if track.id == selected_audio_track_id:
                    selected_index = index
            self.audio_combo.setEnabled(len(ytdlp_tracks) > 1)
            self.audio_combo.setCurrentIndex(selected_index)
        elif len(tracks) > 1:
            for track in tracks:
                self.audio_combo.addItem(track.label, ("track", track.id))
            self.audio_combo.setEnabled(True)
            self.audio_combo.setCurrentIndex(0)
        else:
            self.audio_combo.setEnabled(False)
            self.audio_combo.setCurrentIndex(0)
        self.audio_combo.blockSignals(False)
        self._refresh_width_adaptive_control_visibility()

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
            self._refresh_width_adaptive_control_visibility()
            return
        selected_index = 0
        for index, quality in enumerate(qualities):
            self.video_quality_combo.addItem(quality.label, quality.id)
            if quality.id == selected_quality_id:
                selected_index = index
        self.video_quality_combo.setCurrentIndex(selected_index)
        self.video_quality_combo.setEnabled(len(qualities) > 1)
        self.video_quality_combo.blockSignals(False)
        self._refresh_width_adaptive_control_visibility()

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
        if self._current_item_has_ytdlp_audio_tracks():
            self.audio_combo.blockSignals(True)
            try:
                self.audio_combo.setCurrentIndex(self._selected_ytdlp_audio_combo_index())
            finally:
                self.audio_combo.blockSignals(False)
            return
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
            if self._primary_external_subtitle_track_is_stale():
                self._invalidate_primary_external_subtitle_track()
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
                elif self._should_reload_primary_external_subtitle_for_preference(current_external_subtitle):
                    if not self._reload_selected_primary_external_subtitle_if_needed():
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
        if self._danmaku_temp_path_is_ephemeral and self._danmaku_temp_path is not None:
            try:
                self._danmaku_temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        self._danmaku_temp_path = None
        self._danmaku_temp_path_is_ephemeral = False

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

    def _apply_main_subtitle_ass_override_for_danmaku(self) -> None:
        if (
            not hasattr(self.video, "set_subtitle_ass_override")
            or not getattr(self.video, "supports_subtitle_ass_override", lambda: False)()
        ):
            return
        if self._danmaku_restore_main_ass_override is None and hasattr(self.video, "subtitle_ass_override"):
            try:
                self._danmaku_restore_main_ass_override = self.video.subtitle_ass_override()
            except Exception as exc:
                self._append_log(f"主字幕样式读取失败: {exc}")
                self._danmaku_restore_main_ass_override = "scale"
        try:
            self.video.set_subtitle_ass_override("no")
        except Exception as exc:
            self._append_log(f"弹幕样式设置失败: {exc}")

    def _apply_secondary_subtitle_ass_override_for_danmaku(self) -> None:
        if (
            not hasattr(self.video, "set_secondary_subtitle_ass_override")
            or not getattr(self.video, "supports_secondary_subtitle_ass_override", lambda: False)()
        ):
            return
        if (
            self._danmaku_restore_secondary_ass_override is None
            and hasattr(self.video, "secondary_subtitle_ass_override")
        ):
            try:
                self._danmaku_restore_secondary_ass_override = self.video.secondary_subtitle_ass_override()
            except Exception as exc:
                self._append_log(f"次字幕样式读取失败: {exc}")
                self._danmaku_restore_secondary_ass_override = "scale"
        try:
            self.video.set_secondary_subtitle_ass_override("no")
        except Exception as exc:
            self._append_log(f"弹幕样式设置失败: {exc}")

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
        self._pending_danmaku_render_item = None
        self._danmaku_render_request_id += 1
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
        temp_path = self._build_danmaku_subtitle_file(
            xml_text,
            line_count,
            render_mode=self._preferred_danmaku_render_mode(),
            color_mode=self._preferred_danmaku_color_mode(),
            uniform_color=self._preferred_danmaku_uniform_color(),
            position_preset=self._preferred_danmaku_position_preset(),
            scroll_speed=self._preferred_danmaku_scroll_speed(),
            font_size=self._preferred_danmaku_font_size(),
            opacity=self._preferred_danmaku_opacity(),
            outline_strength=self._preferred_danmaku_outline_strength(),
        )
        if temp_path is None:
            return None
        return temp_path

    def _prepare_danmaku_subtitle_load_path(self, subtitle_path: Path) -> Path:
        suffix = subtitle_path.suffix or ".ass"
        temp_file = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            temp_file.write(subtitle_path.read_bytes())
        finally:
            temp_file.close()
        self._danmaku_temp_path = Path(temp_file.name)
        self._danmaku_temp_path_is_ephemeral = True
        return self._danmaku_temp_path

    def _build_danmaku_subtitle_file(
        self,
        xml_text: str,
        line_count: int,
        *,
        render_mode: str,
        color_mode: str,
        uniform_color: str,
        position_preset: str,
        scroll_speed: float,
        font_size: int,
        opacity: int = 85,
        outline_strength: str = "strong",
    ) -> Path | None:
        intro_episode_label = ""
        current_item = self._current_play_item()
        if current_item is not None:
            episode_number = infer_playlist_episode_number(current_item, self.session.playlist if self.session else None)
            if episode_number is not None and episode_number > 0:
                intro_episode_label = f"第{episode_number}集"
        return load_or_create_danmaku_ass_cache(
            xml_text,
            line_count,
            intro_episode_label=intro_episode_label,
            render_mode=render_mode,
            color_mode=color_mode,
            uniform_color=uniform_color,
            position_preset=position_preset,
            scroll_speed=scroll_speed,
            font_size=font_size,
            opacity=opacity,
            outline_strength=outline_strength,
            time_offset_seconds=current_item.danmaku_offset_seconds if current_item is not None else 0.0,
        )

    def _current_danmaku_render_settings(self) -> dict[str, object]:
        return {
            "render_mode": self._preferred_danmaku_render_mode(),
            "color_mode": self._preferred_danmaku_color_mode(),
            "uniform_color": self._preferred_danmaku_uniform_color(),
            "position_preset": self._preferred_danmaku_position_preset(),
            "scroll_speed": self._preferred_danmaku_scroll_speed(),
            "font_size": self._preferred_danmaku_font_size(),
            "opacity": self._preferred_danmaku_opacity(),
            "outline_strength": self._preferred_danmaku_outline_strength(),
        }

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

    def _attach_danmaku_subtitle_file(self, subtitle_path: Path, line_count: int, *, use_secondary: bool) -> None:
        self._clear_active_danmaku(restore_position=False)
        load_path = self._prepare_danmaku_subtitle_load_path(subtitle_path)
        if not hasattr(self.video, "load_external_subtitle"):
            raise RuntimeError("播放器不支持外挂弹幕")
        track_id = self._load_danmaku_subtitle(load_path, use_secondary=use_secondary)
        if track_id is None:
            raise RuntimeError("播放器未返回弹幕轨道")
        self._danmaku_track_id = track_id
        self._danmaku_active = True
        self._danmaku_line_count = line_count

    def _should_render_danmaku_async(self) -> bool:
        return self.video is self.video_widget

    def _start_async_danmaku_render(self, xml_text: str, line_count: int) -> None:
        if self.session is None:
            return
        current_item = self.session.playlist[self.current_index]
        settings = self._current_danmaku_render_settings()
        self._danmaku_render_request_id += 1
        request_id = self._danmaku_render_request_id
        self._pending_danmaku_render_item = current_item

        def run() -> None:
            try:
                subtitle_path = self._build_danmaku_subtitle_file(
                    xml_text,
                    line_count,
                    render_mode=str(settings["render_mode"]),
                    color_mode=str(settings["color_mode"]),
                    uniform_color=str(settings["uniform_color"]),
                    position_preset=str(settings["position_preset"]),
                    scroll_speed=float(settings["scroll_speed"]),
                    font_size=int(settings["font_size"]),
                    opacity=int(settings["opacity"]),
                    outline_strength=str(settings["outline_strength"]),
                )
            except Exception as exc:
                if self._is_window_alive():
                    self._danmaku_render_signals.failed.emit(request_id, str(exc))
                return
            if self._is_window_alive():
                self._danmaku_render_signals.succeeded.emit(request_id, str(subtitle_path or ""), line_count)

        threading.Thread(target=run, daemon=True).start()

    def _handle_danmaku_render_succeeded(self, request_id: int, subtitle_path_text: str, line_count: int) -> None:
        pending_item = self._pending_danmaku_render_item
        if request_id != self._danmaku_render_request_id or pending_item is None:
            return
        if self.session is None or not (0 <= self.current_index < len(self.session.playlist)):
            self._pending_danmaku_render_item = None
            return
        if self.session.playlist[self.current_index] is not pending_item:
            return
        self._pending_danmaku_render_item = None
        try:
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
            use_secondary = self._danmaku_should_use_secondary_slot()
            if use_secondary:
                self._apply_secondary_subtitle_ass_override_for_danmaku()
            else:
                self._apply_main_subtitle_ass_override_for_danmaku()
            if not subtitle_path_text:
                raise ValueError("弹幕为空")
            self._attach_danmaku_subtitle_file(Path(subtitle_path_text), line_count, use_secondary=use_secondary)
        except Exception as exc:
            if self._should_retry_danmaku_load(exc):
                self._schedule_danmaku_retry()
                return
            self._append_log(f"弹幕加载失败: {exc}")
            self._clear_active_danmaku()
            self._reset_danmaku_combo(enabled=True, current_index=1)

    def _handle_danmaku_render_failed(self, request_id: int, message: str) -> None:
        if request_id != self._danmaku_render_request_id:
            return
        self._pending_danmaku_render_item = None
        self._append_log(f"弹幕加载失败: {message}")
        self._clear_active_danmaku()
        self._reset_danmaku_combo(enabled=True, current_index=1)

    def _enable_danmaku(self, line_count: int) -> None:
        xml_text = self._current_play_item_danmaku_xml()
        if not xml_text:
            return
        if self._should_render_danmaku_async():
            self._start_async_danmaku_render(xml_text, line_count)
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
        use_secondary = self._danmaku_should_use_secondary_slot()
        if use_secondary:
            self._apply_secondary_subtitle_ass_override_for_danmaku()
        else:
            self._apply_main_subtitle_ass_override_for_danmaku()
        subtitle_path = self._write_danmaku_subtitle_file(xml_text, line_count)
        if subtitle_path is None:
            raise ValueError("弹幕为空")
        self._attach_danmaku_subtitle_file(subtitle_path, line_count, use_secondary=use_secondary)

    def _danmaku_should_use_secondary_slot(self) -> bool:
        # 弹幕与内嵌字幕同时存在时，弹幕让出主字幕轨、改用次字幕轨，
        # 这样主字幕轨可自动选中（简体）中文字幕；没有内嵌字幕时仍占用主字幕轨。
        try:
            tracks = self.video.subtitle_tracks()
        except Exception:
            return False
        return bool(tracks)

    def _load_danmaku_subtitle(self, subtitle_path: Path, *, use_secondary: bool) -> int | None:
        self._danmaku_loading_slot = "secondary" if use_secondary else "primary"
        try:
            track_id = self.video.load_external_subtitle(
                str(subtitle_path), select_for_secondary=use_secondary
            )
            # select_for_secondary=True 时 load_external_subtitle 已自行设为次字幕轨，
            # 仅主字幕轨需要显式 apply。
            if (
                track_id is not None
                and not use_secondary
                and hasattr(self.video, "apply_subtitle_mode")
            ):
                self.video.apply_subtitle_mode("track", track_id=track_id)
        finally:
            self._danmaku_loading_slot = None
        self._danmaku_uses_secondary_slot = use_secondary
        return track_id

    def _install_danmaku_log_handler(self, session) -> None:
        controller = getattr(session, "danmaku_controller", None)
        if controller is None:
            return
        setter = getattr(controller, "set_danmaku_log_handler", None)
        if not callable(setter):
            return
        setter(self._danmaku_playback_log_signals.log.emit)

    def _uninstall_danmaku_log_handler(self) -> None:
        if self.session is None:
            return
        controller = getattr(self.session, "danmaku_controller", None)
        if controller is None:
            return
        setter = getattr(controller, "set_danmaku_log_handler", None)
        if not callable(setter):
            return
        setter(None)

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
        if self._primary_external_subtitle_track_is_stale():
            self._invalidate_primary_external_subtitle_track()
            manual_switch_refresh = False
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
                if self._primary_external_subtitle_track_is_stale():
                    self._invalidate_primary_external_subtitle_track()
                if self._reload_selected_primary_external_subtitle_if_needed():
                    return
                if self._primary_external_subtitle_track_needs_reapply():
                    if not self._apply_primary_external_subtitle_track(self._primary_external_subtitle_track_id):
                        return
                    self._sync_subtitle_combo_without_tracks()
                    return
                if self._auto_apply_ytdlp_default_subtitle_if_needed():
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
        if self._current_item_has_ytdlp_audio_tracks():
            self._audio_preference = AudioPreference()
            return
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
            self._start_external_subtitle_fetch(
                external_subtitle,
                secondary=False,
                purpose="primary-manual",
                previous_track_id=self._primary_external_subtitle_track_id,
            )
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
                (quality for quality in current_item.playback_qualities if quality.id == target_quality_id),
                None,
            )
            if selected_quality is None:
                return
            try:
                start_position_seconds = int(self.video.position_seconds() or 0)
            except Exception:
                start_position_seconds = 0
            if not selected_quality.url:
                if (
                    self.session.playback_loader is None
                    or not current_item.original_url
                    or not target_quality_id.startswith("ytdlp_")
                ):
                    return
                can_switch_via_selected_ytdl_format = bool(selected_quality.ytdl_format) and (
                    str(current_item.url or "").strip() == str(current_item.original_url or "").strip()
                ) and bool(str(current_item.ytdl_format or "").strip()) and not current_item.audio_tracks
                if can_switch_via_selected_ytdl_format:
                    previous_url = current_item.url
                    previous_audio_url = current_item.audio_url
                    previous_ytdl_format = current_item.ytdl_format
                    previous_selected_quality_id = current_item.selected_playback_quality_id
                    current_item.url = current_item.original_url
                    current_item.audio_url = ""
                    current_item.ytdl_format = selected_quality.ytdl_format
                    current_item.selected_playback_quality_id = target_quality_id
                    self._refresh_video_quality_state()
                    try:
                        self._start_current_item_playback(
                            start_position_seconds=start_position_seconds,
                            pause=not self.is_playing,
                        )
                    except Exception as exc:
                        current_item.url = previous_url
                        current_item.audio_url = previous_audio_url
                        current_item.ytdl_format = previous_ytdl_format
                        current_item.selected_playback_quality_id = previous_selected_quality_id
                        self._refresh_video_quality_state()
                        self._append_log(f"清晰度切换失败: {exc}")
                    return
                if (
                    current_item.audio_tracks
                    and self.session.playback_loader is not None
                    and current_item.original_url
                    and target_quality_id.startswith("ytdlp_")
                ):
                    previous_url = current_item.url
                    previous_audio_url = current_item.audio_url
                    previous_ytdl_format = current_item.ytdl_format
                    previous_selected_quality_id = current_item.selected_playback_quality_id
                    current_item.url = ""
                    current_item.audio_url = ""
                    current_item.ytdl_format = ""
                    current_item.selected_playback_quality_id = target_quality_id
                    self._refresh_video_quality_state()
                    try:
                        self._play_item_at_index(
                            self.current_index,
                            start_position_seconds=start_position_seconds,
                            pause=not self.is_playing,
                            preserve_primary_external_subtitle_selection=True,
                        )
                    except Exception as exc:
                        current_item.url = previous_url
                        current_item.audio_url = previous_audio_url
                        current_item.ytdl_format = previous_ytdl_format
                        current_item.selected_playback_quality_id = previous_selected_quality_id
                        self._refresh_video_quality_state()
                        self._append_log(f"清晰度切换失败: {exc}")
                    return
                previous_url = current_item.url
                previous_original_url = current_item.original_url
                previous_selected_quality_id = current_item.selected_playback_quality_id
                current_item.url = ""
                current_item.selected_playback_quality_id = target_quality_id
                self._refresh_video_quality_state()
                try:
                    self._play_item_at_index(
                        self.current_index,
                        start_position_seconds=start_position_seconds,
                        pause=not self.is_playing,
                        preserve_primary_external_subtitle_selection=True,
                    )
                except Exception as exc:
                    current_item.url = previous_url
                    current_item.original_url = previous_original_url
                    current_item.selected_playback_quality_id = previous_selected_quality_id
                    self._refresh_video_quality_state()
                    self._append_log(f"清晰度切换失败: {exc}")
                return
            previous_url = current_item.url
            previous_original_url = current_item.original_url
            previous_selected_quality_id = current_item.selected_playback_quality_id
            previous_ytdl_format = current_item.ytdl_format
            previous_audio_url = current_item.audio_url
            current_item.url = selected_quality.url
            if not target_quality_id.startswith("ytdlp_"):
                current_item.original_url = selected_quality.url
            current_item.selected_playback_quality_id = target_quality_id
            current_item.ytdl_format = ""
            current_item.audio_url = ""
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
                current_item.ytdl_format = previous_ytdl_format
                current_item.audio_url = previous_audio_url
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
            if self._current_item_has_ytdlp_audio_tracks():
                target_index = self._selected_ytdlp_audio_combo_index()
                self.audio_combo.blockSignals(True)
                try:
                    self.audio_combo.setCurrentIndex(target_index)
                finally:
                    self.audio_combo.blockSignals(False)
                return
            self._audio_preference = AudioPreference()
            self.video.apply_audio_mode("auto")
            return
        if mode == "ytdlp":
            self._change_ytdlp_audio_selection(str(track_id or ""))
            return
        track = next((track for track in self._audio_tracks if track.id == track_id), None)
        if track is None:
            return
        self._remember_audio_track_preference(track)
        self.video.apply_audio_mode("track", track_id=track_id)

    def _change_ytdlp_audio_selection(self, track_id: str) -> None:
        if self.session is None:
            return
        current_item = self._current_play_item()
        if current_item is None:
            return
        selected_audio_track_id = str(track_id or "").strip()
        if not selected_audio_track_id or selected_audio_track_id == str(current_item.selected_audio_track_id or "").strip():
            return
        if self.session.playback_loader is None:
            return
        try:
            start_position_seconds = int(self.video.position_seconds() or 0)
        except Exception:
            start_position_seconds = 0
        previous_selected_audio_track_id = current_item.selected_audio_track_id
        previous_url = current_item.url
        previous_audio_url = current_item.audio_url
        previous_ytdl_format = current_item.ytdl_format
        current_item.selected_audio_track_id = selected_audio_track_id
        current_item.url = ""
        current_item.audio_url = ""
        current_item.ytdl_format = ""
        try:
            self._play_item_at_index(
                self.current_index,
                start_position_seconds=start_position_seconds,
                pause=not self.is_playing,
                preserve_primary_external_subtitle_selection=True,
            )
        except Exception as exc:
            current_item.selected_audio_track_id = previous_selected_audio_track_id
            current_item.url = previous_url
            current_item.audio_url = previous_audio_url
            current_item.ytdl_format = previous_ytdl_format
            self._refresh_audio_state()
            self._append_log(f"音轨切换失败: {exc}")

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

    def _release_focus_for_video_press(self) -> None:
        """视频控件为 NoFocus,点击它 Qt 不会转移焦点,需主动让当前控件失焦。"""
        focused = self.focusWidget()
        if focused is not None and focused.window() is self:
            focused.clearFocus()

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
        action = self._always_on_top_menu_action
        if action is not None and action.parent() is menu:
            self._always_on_top_menu_action = None

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

    # ---- 外部字幕站搜索 ----

    def _subtitle_search_service(self):
        if self.session is None:
            return None
        return getattr(self.session, "subtitle_search_service", None)

    def _open_subtitle_search_dialog(self) -> None:
        if self._subtitle_search_service() is None:
            self._append_log("字幕搜索不可用")
            return
        dialog = self._ensure_subtitle_search_dialog()
        self._reset_subtitle_search_if_context_changed()
        self._refresh_subtitle_search_context()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        # 首次打开（或换了播放项）自动按当前播放项搜一次，省掉一次手动点击
        if not self._subtitle_search_items:
            self._start_subtitle_search()

    def _ensure_subtitle_search_dialog(self) -> QDialog:
        if self._subtitle_search_dialog is not None:
            return self._subtitle_search_dialog
        dialog = _PlayerToolDialog(title="外部字幕", parent=self, size=(820, 520))
        host = dialog.content_widget()
        layout = dialog.content_layout()

        self._subtitle_search_context_label = QLabel("", host)
        self._subtitle_search_context_label.setWordWrap(True)
        layout.addWidget(self._subtitle_search_context_label)

        search_row = QGridLayout()
        search_row.setHorizontalSpacing(6)
        search_row.setVerticalSpacing(6)
        search_row.addWidget(QLabel("片名", host), 0, 0)
        self._subtitle_search_title_edit = QLineEdit(host)
        self._subtitle_search_title_edit.returnPressed.connect(
            self._start_subtitle_search
        )
        search_row.addWidget(self._subtitle_search_title_edit, 1, 0)
        search_row.addWidget(QLabel("语言", host), 0, 1)
        self._subtitle_search_language_combo = FlatComboBox(host)
        for code, label in _SUBTITLE_LANGUAGE_FILTERS:
            self._subtitle_search_language_combo.addItem(label, code)
        self._subtitle_search_language_combo.currentIndexChanged.connect(
            lambda _index: self._populate_subtitle_search_table()
        )
        search_row.addWidget(self._subtitle_search_language_combo, 1, 1)
        search_row.addWidget(QLabel("字幕站", host), 0, 2)
        self._subtitle_search_provider_combo = FlatComboBox(host)
        self._subtitle_search_provider_combo.addItem("全部", "")
        service = self._subtitle_search_service()
        if service is not None:
            for provider_id in service.provider_order:
                self._subtitle_search_provider_combo.addItem(
                    service.provider_label(provider_id), provider_id
                )
        search_row.addWidget(self._subtitle_search_provider_combo, 1, 2)
        self._subtitle_search_button = QPushButton("搜索字幕", host)
        self._subtitle_search_button.clicked.connect(self._start_subtitle_search)
        search_row.addWidget(self._subtitle_search_button, 1, 3)
        search_row.setColumnStretch(0, 3)
        search_row.setColumnStretch(1, 1)
        search_row.setColumnStretch(2, 1)
        layout.addLayout(search_row)

        # 媒体 ID 行：填了之后 SubDL / OpenSubtitles 会优先按 ID 搜，命中率远高于片名，
        # 尤其是中文片名在英文站搜不到时（如"方舟一号"在 SubDL 搜不到，但用 TMDB ID 能搜到）
        id_row = QHBoxLayout()
        id_row.setSpacing(6)
        id_row.addWidget(QLabel("TMDB ID", host))
        self._subtitle_search_tmdb_id_edit = QLineEdit(host)
        self._subtitle_search_tmdb_id_edit.setPlaceholderText("可选，如 105923")
        self._subtitle_search_tmdb_id_edit.setFixedWidth(160)
        id_row.addWidget(self._subtitle_search_tmdb_id_edit)
        id_row.addWidget(QLabel("IMDb ID", host))
        self._subtitle_search_imdb_id_edit = QLineEdit(host)
        self._subtitle_search_imdb_id_edit.setPlaceholderText("可选，如 tt1234567")
        self._subtitle_search_imdb_id_edit.setFixedWidth(180)
        id_row.addWidget(self._subtitle_search_imdb_id_edit)
        id_row.addStretch(1)
        layout.addLayout(id_row)

        table = QTableWidget(0, len(_SUBTITLE_SEARCH_COLUMNS), host)
        table.setHorizontalHeaderLabels(list(_SUBTITLE_SEARCH_COLUMNS))
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.itemDoubleClicked.connect(lambda _item: self._download_selected_subtitle())
        configure_table_columns(table, _SUBTITLE_SEARCH_NAME_COLUMN)
        self._subtitle_search_table = table
        layout.addWidget(table, 1)

        self._subtitle_search_status_label = QLabel("", host)
        self._subtitle_search_status_label.setWordWrap(True)
        layout.addWidget(self._subtitle_search_status_label)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self._subtitle_search_secondary_button = QPushButton("设为次字幕", host)
        self._subtitle_search_secondary_button.clicked.connect(
            lambda: self._download_selected_subtitle(secondary=True)
        )
        actions.addWidget(self._subtitle_search_secondary_button)
        self._subtitle_search_apply_button = QPushButton("下载并加载", host)
        self._subtitle_search_apply_button.clicked.connect(
            self._download_selected_subtitle
        )
        actions.addWidget(self._subtitle_search_apply_button)
        close_button = QPushButton("关闭", host)
        close_button.clicked.connect(dialog.close)
        actions.addWidget(close_button)
        layout.addLayout(actions)

        self._subtitle_search_dialog = dialog
        self._apply_theme()
        return dialog

    def _subtitle_search_query_context(self) -> tuple[str, str, int | None]:
        """返回 (片名, 发布文件名, 集数)。"""
        item = self._current_play_item()
        vod_name = str(self.session.vod.vod_name or "").strip() if self.session else ""
        item_title = str(getattr(item, "title", "") or "").strip() if item else ""
        # original_title 通常是原始文件名，最适合拿去解析画质/压制组
        file_name = str(getattr(item, "original_title", "") or "").strip() if item else ""
        if not file_name and item is not None:
            path = str(getattr(item, "path", "") or "").strip()
            file_name = path.replace("\\", "/").rsplit("/", 1)[-1]
        title = vod_name or item_title
        episode = None
        if item is not None:
            playlist = self.session.playlist if self.session is not None else None
            episode = infer_playlist_episode_number(item, playlist)
        return title, file_name, episode

    def _build_subtitle_search_query(self):
        title, file_name, episode = self._subtitle_search_query_context()
        if self._subtitle_search_title_edit is not None:
            typed = self._subtitle_search_title_edit.text().strip()
            if typed:
                title = typed
        imdb_id = self._subtitle_edit_value(self._subtitle_search_imdb_id_edit)
        tmdb_id = self._subtitle_edit_value(self._subtitle_search_tmdb_id_edit)
        vod = self.session.vod if self.session is not None else None
        try:
            year = int(str(getattr(vod, "vod_year", "") or "").strip()[:4])
        except (TypeError, ValueError):
            year = 0
        return build_subtitle_query(
            title=title,
            file_name=file_name,
            episode=episode,
            year=year,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
        )

    @staticmethod
    def _subtitle_edit_value(edit: QLineEdit | None) -> str:
        if edit is None:
            return ""
        return edit.text().strip()

    def _resolve_subtitle_search_media_ids(self) -> tuple[str, str]:
        """从已刮削绑定的元数据里取 TMDB / IMDb id，用于自动填充。

        刮削来源通常是 TMDB（provider == "tmdb"）或豆瓣（provider == "douban"）。
        IMDb id 一般拿不到，这里主要补 TMDB id。
        """
        if self.session is None:
            return "", ""
        bindings = getattr(self.session, "metadata_binding_repository", None)
        vod = self.session.vod
        title = str(getattr(vod, "vod_name", "") or "").strip()
        if bindings is None or not title or not hasattr(bindings, "load_by_title"):
            return "", ""
        try:
            binding = bindings.load_by_title(title)
        except Exception:
            return "", ""
        if binding is None:
            return "", ""
        provider = str(getattr(binding, "provider", "") or "").strip().lower()
        provider_id = str(getattr(binding, "provider_id", "") or "").strip()
        if provider == "tmdb" and provider_id:
            return provider_id, ""
        return "", ""

    def _reset_subtitle_search_if_context_changed(self) -> None:
        """切换播放项后清掉过期的片名/TMDB id/搜索结果。

        只清自动填充的内容；用户手改过的片名和 id 保留，不打断手动搜索。
        """
        context = self._subtitle_search_query_context()
        if self._subtitle_search_last_context == context:
            return
        self._subtitle_search_last_context = context
        self._subtitle_search_items = []
        self._subtitle_search_result = None
        if self._subtitle_search_table is not None:
            self._subtitle_search_table.setRowCount(0)
        title_edit = self._subtitle_search_title_edit
        if title_edit is not None:
            current = title_edit.text().strip()
            if not current or current == self._subtitle_search_auto_title:
                title_edit.setText("")
        tmdb_edit = self._subtitle_search_tmdb_id_edit
        if tmdb_edit is not None:
            current = tmdb_edit.text().strip()
            if not current or current == self._subtitle_search_auto_tmdb_id:
                tmdb_edit.setText("")
        self._set_subtitle_search_status("")

    def _refresh_subtitle_search_context(self) -> None:
        query = self._build_subtitle_search_query()
        title_edit = self._subtitle_search_title_edit
        if title_edit is not None and not title_edit.text().strip():
            title_edit.setText(query.title)
            self._subtitle_search_auto_title = query.title
        # 自动填充刮削绑定到的 TMDB id（用户没手填时才覆盖）
        tmdb_id, _imdb_id = self._resolve_subtitle_search_media_ids()
        if tmdb_id and self._subtitle_search_tmdb_id_edit is not None:
            if not self._subtitle_search_tmdb_id_edit.text().strip():
                self._subtitle_search_tmdb_id_edit.setText(tmdb_id)
                self._subtitle_search_auto_tmdb_id = tmdb_id
        if self._subtitle_search_context_label is not None:
            parts = [f"影片：{query.title or '未知'}"]
            if query.season is not None and query.episode is not None:
                parts.append(f"S{query.season:02d}E{query.episode:02d}")
            elif query.episode is not None:
                parts.append(f"第 {query.episode} 集")
            extras = [
                value
                for value in (
                    query.resolution,
                    query.source,
                    query.codec,
                    query.release_group,
                )
                if value
            ]
            if extras:
                parts.append(" / ".join(extras))
            self._subtitle_search_context_label.setText("　".join(parts))

    def _set_subtitle_search_busy(self, busy: bool) -> None:
        for button in (
            self._subtitle_search_button,
            self._subtitle_search_apply_button,
            self._subtitle_search_secondary_button,
        ):
            if button is not None:
                button.setEnabled(not busy)

    def _start_subtitle_search(self) -> None:
        service = self._subtitle_search_service()
        if service is None:
            return
        query = self._build_subtitle_search_query()
        if not query.title and not query.file_name:
            self._set_subtitle_search_status("没有可用于搜索的片名")
            return
        provider_filter = ""
        if self._subtitle_search_provider_combo is not None:
            provider_filter = str(
                self._subtitle_search_provider_combo.currentData() or ""
            )
        self._subtitle_search_request_id += 1
        request_id = self._subtitle_search_request_id
        self._set_subtitle_search_status("正在搜索字幕…")
        self._set_subtitle_search_busy(True)

        def run() -> None:
            try:
                result = service.search(query, provider_filter=provider_filter)
            except Exception as exc:
                if self._is_window_alive():
                    self._subtitle_search_signals.failed.emit(
                        request_id, f"字幕搜索失败: {exc}"
                    )
                return
            if self._is_window_alive():
                self._subtitle_search_signals.search_succeeded.emit(request_id, result)

        threading.Thread(target=run, daemon=True).start()

    def _handle_subtitle_search_succeeded(self, request_id: int, result: object) -> None:
        if request_id != self._subtitle_search_request_id:
            return
        self._set_subtitle_search_busy(False)
        self._subtitle_search_result = result
        self._subtitle_search_items = [
            item for group in getattr(result, "groups", []) for item in group.items
        ]
        self._populate_subtitle_search_table()

    def _handle_subtitle_search_failed(self, request_id: int, message: str) -> None:
        if request_id != self._subtitle_search_request_id:
            return
        self._set_subtitle_search_busy(False)
        self._set_subtitle_search_status(message)
        self._append_log(message)

    def _subtitle_search_language_filter(self) -> str:
        if self._subtitle_search_language_combo is None:
            return ""
        return str(self._subtitle_search_language_combo.currentData() or "")

    def _populate_subtitle_search_table(self) -> None:
        table = self._subtitle_search_table
        if table is None:
            return
        language_filter = self._subtitle_search_language_filter()
        rows = [
            item
            for item in self._subtitle_search_items
            if not language_filter or item.language == language_filter
        ]
        table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            values = (
                item.provider_label,
                item.name,
                item.language_label or item.language,
                (item.format or "").upper(),
                f"{item.match_percent}%",
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(str(text))
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, item)
                table.setItem(row, column, cell)
        if rows:
            table.selectRow(0)
        configure_table_columns(table, _SUBTITLE_SEARCH_NAME_COLUMN)
        self._set_subtitle_search_status(self._describe_subtitle_search_result(len(rows)))

    def _describe_subtitle_search_result(self, shown: int) -> str:
        result = self._subtitle_search_result
        service = self._subtitle_search_service()
        errors = getattr(result, "errors", {}) or {} if result else {}
        skipped = getattr(result, "skipped", []) or [] if result else []
        # 没有任何结果时，给出可操作的根因提示，而不是干巴巴的"共 0 条"
        if shown == 0:
            if skipped:
                labels = "、".join(
                    service.provider_label(key) if service else key for key in skipped
                )
                return (
                    f"没有找到字幕。以下站点未配置 Token 已跳过：{labels}。"
                    "免 Token 站中字幕库当前已不可用（SubHD 仍可用）。"
                    "推荐在「高级设置 → 字幕」配置射手网(ASSRT)的免费 Token "
                    "（assrt.net 注册即得），它的中文字幕最全；"
                    "也可配置 SubSource（subsource.net）的免费 API Key。"
                )
            if errors:
                details = "；".join(
                    f"{service.provider_label(key) if service else key}: {message}"
                    for key, message in errors.items()
                )
                return f"没有找到字幕，所有站点均失败（{details}）。可尝试改用英文片名或填写 TMDB/IMDb ID 后重搜。"
            return "没有找到字幕。可尝试改用英文片名，或填写 TMDB/IMDb ID 后重搜。"
        parts = [f"共 {shown} 条"]
        if result is None:
            return parts[0]
        notices = [
            group.notice
            for group in getattr(result, "groups", [])
            if getattr(group, "notice", "")
        ]
        if errors:
            details = "；".join(
                f"{service.provider_label(key) if service else key}: {message}"
                for key, message in errors.items()
            )
            parts.append(f"部分站点失败（{details}）")
        if skipped:
            labels = "、".join(
                service.provider_label(key) if service else key for key in skipped
            )
            parts.append(f"未配置 Token 已跳过：{labels}")
        parts.extend(notices)
        return "　|　".join(parts)

    def _set_subtitle_search_status(self, text: str) -> None:
        if self._subtitle_search_status_label is not None:
            self._subtitle_search_status_label.setText(text)

    def _selected_subtitle_search_item(self):
        table = self._subtitle_search_table
        if table is None:
            return None
        row = table.currentRow()
        if row < 0:
            return None
        cell = table.item(row, 0)
        return cell.data(Qt.ItemDataRole.UserRole) if cell is not None else None

    def _download_selected_subtitle(self, *, secondary: bool = False) -> None:
        service = self._subtitle_search_service()
        item = self._selected_subtitle_search_item()
        if service is None or item is None:
            self._set_subtitle_search_status("请先选择一条字幕")
            return
        self._subtitle_search_request_id += 1
        request_id = self._subtitle_search_request_id
        self._set_subtitle_search_status(f"正在下载：{item.name}")
        self._set_subtitle_search_busy(True)
        title = self._build_subtitle_search_query().title

        def run() -> None:
            try:
                content = service.download(item)
                path = save_subtitle_file(content, title=title or item.name)
            except Exception as exc:
                if self._is_window_alive():
                    self._subtitle_search_signals.failed.emit(
                        request_id, f"字幕下载失败: {exc}"
                    )
                return
            if self._is_window_alive():
                self._subtitle_search_signals.download_succeeded.emit(
                    request_id, item, (str(path), secondary)
                )

        threading.Thread(target=run, daemon=True).start()

    def _handle_subtitle_download_succeeded(
        self,
        request_id: int,
        item: object,
        payload: object,
    ) -> None:
        if request_id != self._subtitle_search_request_id:
            return
        self._set_subtitle_search_busy(False)
        path, secondary = payload
        current_item = self._current_play_item()
        if current_item is None:
            self._set_subtitle_search_status("当前没有播放项，无法加载字幕")
            return
        label = f"{item.provider_label} {item.language_label or item.language}".strip()
        option = ExternalSubtitleOption(
            name=f"{label} · {item.name}".strip(" ·"),
            lang=item.language,
            url=path,
            format=(item.format or "").lstrip("."),
            source="subtitle-site",
        )
        existing = self._find_current_item_external_subtitle(path)
        if existing is None:
            current_item.external_subtitles = [*current_item.external_subtitles, option]
        # 复用既有的外挂字幕通道：下拉框与右键菜单会自动出现这一条
        self._refresh_subtitle_state()
        if secondary:
            self._set_secondary_subtitle_from_menu("external", path)
        else:
            self._set_primary_subtitle_from_menu("external", path)
        self._set_subtitle_search_status(f"已加载：{option.name}")
        self._append_log(f"已加载外部字幕: {option.name}")

    def _build_video_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addMenu(self._build_primary_subtitle_menu(menu))
        menu.addMenu(self._build_secondary_subtitle_menu(menu))
        menu.addMenu(self._build_subtitle_position_menu(menu, title="主字幕位置", secondary=False))
        menu.addMenu(self._build_subtitle_position_menu(menu, title="次字幕位置", secondary=True))
        menu.addMenu(self._build_subtitle_scale_menu(menu, title="主字幕大小", secondary=False))
        menu.addMenu(self._build_subtitle_scale_menu(menu, title="次字幕大小", secondary=True))
        menu.addMenu(self._build_audio_menu(menu))
        menu.addMenu(self._build_subtitle_delay_menu(menu))
        menu.addMenu(self._build_audio_delay_menu(menu))
        menu.addMenu(self._build_picture_menu(menu))
        if self._video_quality_options:
            menu.addMenu(self._build_video_quality_menu(menu))
        menu.addMenu(self._build_danmaku_menu(menu))
        menu.addAction("刮削", self._open_metadata_scrape_dialog)
        menu.addAction("重写剧集标题", self._rerun_episode_title_enhancement)
        menu.addAction("搜索字幕", self._open_subtitle_search_dialog)
        menu.addAction("弹幕源", self._open_danmaku_source_dialog)
        menu.addAction("弹幕设置", self._open_danmaku_settings_dialog)
        menu.addAction("视频信息", self._toggle_video_info_from_menu)
        always_on_top_action = menu.addAction("播放时置顶")
        always_on_top_action.setCheckable(True)
        always_on_top_action.toggled.connect(
            lambda checked, action=always_on_top_action: self._set_always_on_top(
                checked,
                menu_action=action,
            )
        )
        self._always_on_top_menu_action = always_on_top_action
        self._sync_always_on_top_controls(menu_action=always_on_top_action)
        menu.addAction("退出播放", self._return_to_main)
        return menu

    def _current_play_item(self) -> PlayItem | None:
        if self.session is None or not self.session.playlist:
            return None
        if not 0 <= self.current_index < len(self.session.playlist):
            return None
        return self.session.playlist[self.current_index]

    def _show_playlist_context_menu(self, pos) -> None:
        if self.session is None or not self.session.playlist:
            return
        item_at = self.playlist.itemAt(pos)
        if item_at is None:
            return
        row = self.playlist.row(item_at)
        if not 0 <= row < len(self.session.playlist):
            return
        play_item = self.session.playlist[row]
        menu = QMenu(self)
        menu.addAction("编辑分集标题", lambda: self._edit_playlist_item_title(row))
        if self._playlist_item_has_override(play_item):
            menu.addAction("恢复标题", lambda: self._reset_playlist_item_title(row))
        menu.exec(self.playlist.mapToGlobal(pos))

    def _episode_title_override_identity(self) -> tuple[str, str, str] | None:
        if self.session is None:
            return None
        source_kind = str(getattr(self.session, "source_kind", "") or "")
        source_key = str(getattr(self.session, "source_key", "") or "")
        vod_id = str(getattr(self.session.vod, "vod_id", "") or "")
        if not vod_id:
            return None
        return source_kind, source_key, vod_id

    def _session_episode_title_overrides(self) -> dict[str, str]:
        identity = self._episode_title_override_identity()
        repo = getattr(self.session, "episode_title_override_repository", None) if self.session else None
        if identity is None or repo is None:
            return {}
        source_kind, source_key, vod_id = identity
        return repo.load_for_session(source_kind=source_kind, source_key=source_key, vod_id=vod_id)

    def _playlist_item_has_override(self, play_item: PlayItem) -> bool:
        return episode_override_item_key(play_item) in self._session_episode_title_overrides()

    def _apply_episode_title_overrides_to_session(self) -> None:
        """Stamp manual overrides onto the current session playlist (idempotent).

        Used after paths that set episode titles without going through the app
        enhancer (e.g. manual metadata scrape), so a saved override always wins.
        """
        if self.session is None:
            return
        overrides = self._session_episode_title_overrides()
        if overrides:
            apply_episode_title_overrides(self.session.playlist, overrides)

    def _edit_playlist_item_title(self, row: int) -> None:
        if self.session is None or not 0 <= row < len(self.session.playlist):
            return
        repo = getattr(self.session, "episode_title_override_repository", None)
        identity = self._episode_title_override_identity()
        play_item = self.session.playlist[row]
        if identity is None or repo is None:
            self._append_log("当前来源不支持分集标题手动修正")
            return
        current_display = playlist_item_display_title(play_item, "episode").strip()
        original = play_item.original_title.strip() or play_item.title.strip()
        edited, ok = QInputDialog.getText(
            self,
            "编辑分集标题",
            f"原始文件名:\n{original}" if original else "编辑分集标题",
            text=current_display or original,
        )
        if not ok:
            return
        edited = edited.strip()
        if not edited or edited == current_display:
            return
        source_kind, source_key, vod_id = identity
        repo.upsert(
            source_kind=source_kind,
            source_key=source_key,
            vod_id=vod_id,
            item_key=episode_override_item_key(play_item),
            display_title=edited,
        )
        play_item.episode_display_title = edited
        play_item.episode_title_source = "manual"
        self.playlist_title_mode = "episode"
        self._render_playlist_items()

    def _reset_playlist_item_title(self, row: int) -> None:
        if self.session is None or not 0 <= row < len(self.session.playlist):
            return
        repo = getattr(self.session, "episode_title_override_repository", None)
        identity = self._episode_title_override_identity()
        play_item = self.session.playlist[row]
        if identity is None or repo is None:
            return
        source_kind, source_key, vod_id = identity
        repo.delete(
            source_kind=source_kind,
            source_key=source_key,
            vod_id=vod_id,
            item_key=episode_override_item_key(play_item),
        )
        if self.session.episode_title_enhancer is not None:
            # Re-derive titles; the deleted item falls back to its auto/original title.
            self.session.episode_titles_hydrated = False
            self._start_episode_title_enhancement()
        else:
            play_item.episode_display_title = ""
            play_item.episode_title_source = ""
            self._render_playlist_items()

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
        if self._danmaku_opacity_spin is not None:
            self._danmaku_opacity_spin.blockSignals(True)
            self._danmaku_opacity_spin.setValue(self._preferred_danmaku_opacity())
            self._danmaku_opacity_spin.blockSignals(False)
        if self._danmaku_outline_strength_combo is not None:
            self._danmaku_outline_strength_combo.blockSignals(True)
            self._danmaku_outline_strength_combo.setCurrentIndex(
                max(0, self._danmaku_outline_strength_combo.findData(self._preferred_danmaku_outline_strength()))
            )
            self._danmaku_outline_strength_combo.blockSignals(False)
        if self._danmaku_scroll_speed_spin is not None:
            self._danmaku_scroll_speed_spin.blockSignals(True)
            self._danmaku_scroll_speed_spin.setValue(self._preferred_danmaku_scroll_speed())
            self._danmaku_scroll_speed_spin.blockSignals(False)
        self._refresh_danmaku_settings_color_controls()
        self._refresh_danmaku_settings_position_controls()

    def _ensure_danmaku_settings_dialog(self) -> QDialog:
        if self._danmaku_settings_dialog is not None:
            return self._danmaku_settings_dialog
        dialog = _PlayerToolDialog(title="弹幕设置", parent=self, size=(420, 320))
        host = dialog.content_widget()
        layout = dialog.content_layout()
        setting_label_texts = (
            "显示行数",
            "显示模式",
            "位置预设",
            "颜色模式",
            "统一颜色",
            "文字大小",
            "透明度",
            "滚动速率",
        )
        setting_label_width = max(host.fontMetrics().horizontalAdvance(text) for text in setting_label_texts) + 8

        def add_setting_row(label_text: str, field: QWidget) -> None:
            row = QHBoxLayout()
            label = QLabel(label_text, host)
            label.setFixedWidth(setting_label_width)
            row.addWidget(label)
            row.addWidget(field, 1)
            layout.addLayout(row)

        self._danmaku_line_count_spin = QSpinBox(host)
        self._danmaku_line_count_spin.setRange(1, 10)
        add_setting_row("显示行数", self._danmaku_line_count_spin)

        self._danmaku_render_mode_combo = FlatComboBox(host)
        self._danmaku_render_mode_combo.addItem("静态", "static")
        self._danmaku_render_mode_combo.addItem("仅滚动", "scroll_only")
        self._danmaku_render_mode_combo.addItem("混合", "mixed")
        add_setting_row("显示模式", self._danmaku_render_mode_combo)

        self._danmaku_position_preset_combo = FlatComboBox(host)
        self._danmaku_position_preset_combo.addItem("顶部", "top")
        self._danmaku_position_preset_combo.addItem("顶部偏下", "upper")
        self._danmaku_position_preset_combo.addItem("中上", "mid_upper")
        self._danmaku_position_preset_combo.addItem("底部", "bottom")
        add_setting_row("位置预设", self._danmaku_position_preset_combo)

        self._danmaku_color_mode_combo = FlatComboBox(host)
        self._danmaku_color_mode_combo.addItem("统一颜色", "uniform")
        self._danmaku_color_mode_combo.addItem("保留原色", "source")
        add_setting_row("颜色模式", self._danmaku_color_mode_combo)

        self._danmaku_uniform_color_edit = None
        self._danmaku_uniform_color_button = QPushButton(host)
        add_setting_row("统一颜色", self._danmaku_uniform_color_button)

        self._danmaku_font_size_spin = QSpinBox(host)
        self._danmaku_font_size_spin.setRange(16, 72)
        self._danmaku_font_size_spin.setSingleStep(2)
        add_setting_row("文字大小", self._danmaku_font_size_spin)

        self._danmaku_opacity_spin = QSpinBox(host)
        self._danmaku_opacity_spin.setRange(30, 100)
        self._danmaku_opacity_spin.setSingleStep(5)
        self._danmaku_opacity_spin.setSuffix("%")
        add_setting_row("透明度", self._danmaku_opacity_spin)

        self._danmaku_outline_strength_combo = None

        self._danmaku_scroll_speed_spin = QDoubleSpinBox(host)
        self._danmaku_scroll_speed_spin.setRange(0.5, 2.0)
        self._danmaku_scroll_speed_spin.setSingleStep(0.1)
        self._danmaku_scroll_speed_spin.setDecimals(1)
        self._danmaku_scroll_speed_spin.setSuffix("x")
        add_setting_row("滚动速率", self._danmaku_scroll_speed_spin)

        actions = QHBoxLayout()
        actions.addStretch(1)
        reset_button = QPushButton("恢复默认", host)
        close_button = QPushButton("关闭", host)
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
        self._danmaku_opacity_spin.valueChanged.connect(self._save_danmaku_opacity)
        self._danmaku_scroll_speed_spin.valueChanged.connect(self._save_danmaku_scroll_speed)
        reset_button.clicked.connect(self._restore_default_danmaku_render_settings)
        close_button.clicked.connect(dialog.close)

        self._danmaku_settings_dialog = dialog
        self._apply_theme()
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
        self.config.preferred_danmaku_opacity = 85
        self.config.preferred_danmaku_outline_strength = "strong"
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

    def _ensure_metadata_scrape_dialog(self) -> QDialog:
        if self._metadata_scrape_dialog is not None:
            return self._metadata_scrape_dialog
        dialog = _PlayerToolDialog(title="刮削", parent=self, size=(760, 480))
        host = dialog.content_widget()
        layout = dialog.content_layout()

        search_row = QGridLayout()
        search_row.setHorizontalSpacing(6)
        search_row.setVerticalSpacing(6)
        search_row.addWidget(QLabel("标题", host), 0, 0)
        self._metadata_scrape_title_edit = QLineEdit(host)
        search_row.addWidget(self._metadata_scrape_title_edit, 1, 0, alignment=Qt.AlignmentFlag.AlignTop)
        search_row.addWidget(QLabel("年份", host), 0, 1)
        self._metadata_scrape_year_edit = QLineEdit(host)
        search_row.addWidget(self._metadata_scrape_year_edit, 1, 1, alignment=Qt.AlignmentFlag.AlignTop)
        search_row.addWidget(QLabel("分类", host), 0, 2)
        self._metadata_scrape_category_combo = FlatComboBox(host)
        search_row.addWidget(self._metadata_scrape_category_combo, 1, 2, alignment=Qt.AlignmentFlag.AlignTop)
        search_row.addWidget(QLabel("搜索来源", host), 0, 3)
        self._metadata_scrape_provider_combo = FlatComboBox(host)
        search_row.addWidget(self._metadata_scrape_provider_combo, 1, 3, alignment=Qt.AlignmentFlag.AlignTop)
        search_row.setColumnStretch(0, 2)
        search_row.setColumnStretch(1, 1)
        search_row.setColumnStretch(2, 1)
        search_row.setColumnStretch(3, 1)
        layout.addLayout(search_row)

        columns = QHBoxLayout()
        self._metadata_scrape_group_list = QListWidget(host)
        self._metadata_scrape_result_list = QListWidget(host)
        columns.addWidget(self._metadata_scrape_group_list, 1)
        columns.addWidget(self._metadata_scrape_result_list, 2)
        layout.addLayout(columns)

        self._metadata_scrape_status_label = QLabel("", host)
        layout.addWidget(self._metadata_scrape_status_label)

        actions = QHBoxLayout()
        self._metadata_scrape_rerun_button = QPushButton("重新搜索", host)
        self._metadata_scrape_reset_button = QPushButton("自动刮削", host)
        self._metadata_scrape_restore_query_button = QPushButton("恢复默认", host)
        self._metadata_scrape_apply_button = QPushButton("应用结果", host)
        actions.addWidget(self._metadata_scrape_rerun_button)
        actions.addWidget(self._metadata_scrape_reset_button)
        actions.addWidget(self._metadata_scrape_restore_query_button)
        actions.addWidget(self._metadata_scrape_apply_button)
        layout.addLayout(actions)

        self._metadata_scrape_rerun_button.clicked.connect(self._rerun_metadata_scrape_search)
        self._metadata_scrape_reset_button.clicked.connect(self._reset_metadata_scrape_state)
        self._metadata_scrape_restore_query_button.clicked.connect(self._restore_default_metadata_scrape_query)
        self._metadata_scrape_apply_button.clicked.connect(self._apply_selected_metadata_scrape_result)
        self._metadata_scrape_group_list.currentRowChanged.connect(self._populate_metadata_scrape_results)
        self._metadata_scrape_category_combo.currentIndexChanged.connect(self._handle_metadata_scrape_category_changed)
        dialog.finished.connect(lambda _result: self._remember_metadata_scrape_query_state())

        self._metadata_scrape_dialog = dialog
        self._apply_theme()
        self._refresh_metadata_scrape_search_row_heights()
        return dialog

    def _populate_metadata_scrape_category_options(self) -> None:
        if self._metadata_scrape_category_combo is None:
            return
        current_value = str(self._metadata_scrape_category_combo.currentData() or "")
        self._metadata_scrape_category_combo.clear()
        for category_value, category_label in _METADATA_SCRAPE_CATEGORY_OPTIONS:
            self._metadata_scrape_category_combo.addItem(category_label, category_value)
        category_index = max(0, self._metadata_scrape_category_combo.findData(current_value))
        self._metadata_scrape_category_combo.setCurrentIndex(category_index)

    def _metadata_scrape_selected_category_name(self) -> str:
        if self._metadata_scrape_category_combo is not None:
            selected_category = str(self._metadata_scrape_category_combo.currentData() or "").strip()
            if selected_category:
                return selected_category
        if self.session is None:
            return ""
        return str(self.session.vod.category_name or "").strip()

    def _populate_metadata_scrape_provider_options(self, selected_provider: str = "") -> None:
        if self._metadata_scrape_provider_combo is None:
            return
        options = list(_METADATA_PROVIDER_OPTIONS)
        service = self.session.metadata_scrape_service if self.session is not None else None
        provider_options = getattr(service, "provider_options", None)
        if callable(provider_options):
            query = None
            if self.session is not None:
                title, year = self._metadata_scrape_current_query()
                if self._metadata_scrape_title_edit is not None:
                    title = self._metadata_scrape_title_edit.text().strip() or title
                if self._metadata_scrape_year_edit is not None:
                    year = self._metadata_scrape_year_edit.text().strip()
                query = MetadataQuery(
                    title=title,
                    year=year,
                    type_name=str(self.session.vod.type_name or "").strip(),
                    category_name=self._metadata_scrape_selected_category_name(),
                )
            options = [(str(key or "").strip(), str(label or "").strip()) for key, label in provider_options(query)]
            options = [(key, label) for key, label in options if key and label]
        current_provider = selected_provider or str(self._metadata_scrape_provider_combo.currentData() or "")
        self._metadata_scrape_provider_combo.clear()
        self._metadata_scrape_provider_combo.addItem("全部", "")
        for provider_key, provider_label in options:
            self._metadata_scrape_provider_combo.addItem(provider_label, provider_key)
        provider_index = max(0, self._metadata_scrape_provider_combo.findData(current_provider))
        self._metadata_scrape_provider_combo.setCurrentIndex(provider_index)

    def _open_metadata_scrape_dialog(self) -> None:
        if self.session is None or self.session.metadata_scrape_service is None:
            return
        self._metadata_scrape_default_title, self._metadata_scrape_default_year = self._metadata_scrape_current_query()
        if not self._metadata_scrape_binding_title:
            self._metadata_scrape_binding_title = self._metadata_scrape_default_title
        if not self._metadata_scrape_binding_year:
            self._metadata_scrape_binding_year = self._metadata_scrape_default_year
        if not self._metadata_scrape_query_saved:
            self._restore_metadata_scrape_query_state_from_cache()
        saved_title = self._metadata_scrape_saved_title if self._metadata_scrape_query_saved else ""
        saved_year = self._metadata_scrape_saved_year if self._metadata_scrape_query_saved else ""
        saved_category = self._metadata_scrape_saved_category if self._metadata_scrape_query_saved else ""
        title_value = saved_title if saved_title.strip() else self._metadata_scrape_default_title
        year_value = saved_year if (saved_title.strip() or saved_year.strip()) else self._metadata_scrape_default_year
        dialog = self._ensure_metadata_scrape_dialog()
        if self._metadata_scrape_title_edit is not None:
            self._metadata_scrape_title_edit.setText(title_value)
        if self._metadata_scrape_year_edit is not None:
            self._metadata_scrape_year_edit.setText(year_value)
        self._populate_metadata_scrape_category_options()
        if self._metadata_scrape_category_combo is not None:
            category_value = saved_category
            category_index = max(0, self._metadata_scrape_category_combo.findData(category_value))
            self._metadata_scrape_category_combo.setCurrentIndex(category_index)
        provider_value = self._metadata_scrape_saved_provider if self._metadata_scrape_query_saved else ""
        self._populate_metadata_scrape_provider_options(selected_provider=provider_value)
        self._clear_metadata_scrape_search_results()
        if self._metadata_scrape_provider_combo is not None:
            provider_index = max(0, self._metadata_scrape_provider_combo.findData(provider_value))
            self._metadata_scrape_provider_combo.setCurrentIndex(provider_index)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._refresh_metadata_scrape_search_row_heights()
        self._reload_metadata_scrape_cached_results()

    def _metadata_scrape_current_title(self) -> str:
        return self._metadata_scrape_current_query()[0]

    def _metadata_scrape_current_query(self) -> tuple[str, str]:
        current_item = None
        if self.session is not None and 0 <= self.current_index < len(self.session.playlist):
            current_item = self.session.playlist[self.current_index]
        title = str(getattr(current_item, "media_title", "") or "").strip()
        if not title and self.session is not None:
            title = str(self.session.vod.vod_name or "").strip()
        year = str(self.session.vod.vod_year or "").strip() if self.session is not None else ""
        return self._normalize_metadata_scrape_query_inputs(title, year)

    def _normalize_metadata_scrape_query_inputs(self, title: str, year: str) -> tuple[str, str]:
        return normalize_metadata_query_inputs(title, year)

    def _metadata_scrape_provider_label(self, provider_key: str) -> str:
        return "全部" if not provider_key else _metadata_provider_label(provider_key)

    def _handle_metadata_scrape_category_changed(self) -> None:
        if self._metadata_scrape_provider_combo is None:
            return
        self._populate_metadata_scrape_provider_options()

    def _clear_metadata_scrape_search_results(self) -> None:
        self._metadata_scrape_request_id += 1
        self._metadata_scrape_groups = []
        if self._metadata_scrape_group_list is not None:
            self._metadata_scrape_group_list.clear()
        if self._metadata_scrape_result_list is not None:
            self._metadata_scrape_result_list.clear()
        if self._metadata_scrape_status_label is not None:
            self._metadata_scrape_status_label.setText("")

    def _populate_metadata_scrape_groups(self, groups) -> None:
        if self._metadata_scrape_group_list is None:
            return
        self._metadata_scrape_groups = list(groups)
        self._metadata_scrape_group_list.clear()
        for group in self._metadata_scrape_groups:
            self._metadata_scrape_group_list.addItem(f"{group.provider_label} ({len(group.items)})")
        if self._metadata_scrape_groups:
            first_non_empty = next(
                (index for index, group in enumerate(self._metadata_scrape_groups) if group.items),
                0,
            )
            self._metadata_scrape_group_list.setCurrentRow(first_non_empty)

    def _populate_metadata_scrape_results(self, group_index: int) -> None:
        if self._metadata_scrape_result_list is None:
            return
        self._metadata_scrape_result_list.clear()
        if group_index < 0 or group_index >= len(self._metadata_scrape_groups):
            return
        group = self._metadata_scrape_groups[group_index]
        for candidate in group.items:
            label = candidate.title if not candidate.year else f"{candidate.title} ({candidate.year})"
            if str(candidate.subtitle or "").strip():
                label = f"{label} · {candidate.subtitle.strip()}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, candidate)
            self._metadata_scrape_result_list.addItem(item)
        if self._metadata_scrape_result_list.count():
            self._metadata_scrape_result_list.setCurrentRow(0)

    def _start_metadata_scrape_search(
        self,
        *,
        title: str,
        year: str,
        category_name: str,
        provider_filter: str,
        cache_only: bool,
        status_text: str | None,
    ) -> None:
        if self.session is None or self.session.metadata_scrape_service is None:
            return
        if self._metadata_scrape_status_label is not None and status_text is not None:
            self._metadata_scrape_status_label.setText(status_text)
        self._metadata_scrape_request_id += 1
        request_id = self._metadata_scrape_request_id
        service = self.session.metadata_scrape_service
        query = MetadataQuery(
            title=title,
            year=year,
            category_name=category_name,
            type_name=str(self.session.vod.type_name or "").strip(),
        )

        def run() -> None:
            try:
                groups = service.search(query, provider_filter=provider_filter, cache_only=cache_only)
            except Exception as exc:
                if self._is_window_alive():
                    self._metadata_scrape_signals.failed.emit(request_id, f"刮削搜索失败: {exc}")
                return
            if self._is_window_alive():
                self._metadata_scrape_signals.search_succeeded.emit(request_id, groups)

        threading.Thread(target=run, daemon=True).start()

    def _reload_metadata_scrape_cached_results(self) -> None:
        if (
            self.session is None
            or self.session.metadata_scrape_service is None
            or self._metadata_scrape_title_edit is None
            or self._metadata_scrape_year_edit is None
            or self._metadata_scrape_provider_combo is None
        ):
            return
        title, year = self._normalize_metadata_scrape_query_inputs(
            self._metadata_scrape_title_edit.text().strip(),
            self._metadata_scrape_year_edit.text().strip() if self._metadata_scrape_year_edit is not None else "",
        )
        if not title:
            if self._metadata_scrape_status_label is not None:
                self._metadata_scrape_status_label.setText("当前条目缺少标题")
            return
        logger.info(
            "Metadata scrape dialog cached reload raw_title=%s raw_year=%s normalized_title=%s normalized_year=%s category=%s provider=%s",
            self._metadata_scrape_title_edit.text().strip(),
            self._metadata_scrape_year_edit.text().strip() if self._metadata_scrape_year_edit is not None else "",
            title,
            year,
            self._metadata_scrape_selected_category_name(),
            str(self._metadata_scrape_provider_combo.currentData() or ""),
            extra={"log_category": "metadata", "log_source": "app"},
        )
        self._start_metadata_scrape_search(
            title=title,
            year=year,
            category_name=self._metadata_scrape_selected_category_name(),
            provider_filter=str(self._metadata_scrape_provider_combo.currentData() or ""),
            cache_only=True,
            status_text=None,
        )

    def _rerun_metadata_scrape_search(self) -> None:
        if (
            self.session is None
            or self.session.metadata_scrape_service is None
            or self._metadata_scrape_title_edit is None
            or self._metadata_scrape_year_edit is None
            or self._metadata_scrape_category_combo is None
            or self._metadata_scrape_provider_combo is None
            or self._metadata_scrape_status_label is None
        ):
            return
        title, year = self._normalize_metadata_scrape_query_inputs(
            self._metadata_scrape_title_edit.text().strip(),
            self._metadata_scrape_year_edit.text().strip(),
        )
        if title != self._metadata_scrape_title_edit.text().strip():
            self._metadata_scrape_title_edit.setText(title)
        if year != self._metadata_scrape_year_edit.text().strip():
            self._metadata_scrape_year_edit.setText(year)
        self._remember_metadata_scrape_query_state()
        if not title:
            self._metadata_scrape_status_label.setText("当前条目缺少标题")
            return
        category_name = self._metadata_scrape_selected_category_name()
        provider_filter = str(self._metadata_scrape_provider_combo.currentData() or "")
        logger.info(
            "Metadata scrape dialog rerun raw_title=%s raw_year=%s normalized_title=%s normalized_year=%s category=%s provider=%s",
            self._metadata_scrape_title_edit.text().strip(),
            self._metadata_scrape_year_edit.text().strip(),
            title,
            year,
            category_name,
            provider_filter,
            extra={"log_category": "metadata", "log_source": "app"},
        )
        self._start_metadata_scrape_search(
            title=title,
            year=year,
            category_name=category_name,
            provider_filter=provider_filter,
            cache_only=False,
            status_text=f"刮削搜索中（{self._metadata_scrape_provider_label(provider_filter)}）...",
        )

    def _reset_metadata_scrape_search_query(self) -> None:
        self._rerun_metadata_scrape_search()

    def _metadata_scrape_reset_vod_id(self) -> str:
        if self.session is None:
            return ""
        vod_id = str(self.session.vod.vod_id or "").strip()
        if not self._is_bilibili_metadata_session():
            return vod_id
        for pattern in (_BILIBILI_SS_ID_RE, _BILIBILI_SEASON_ID_RE):
            if pattern.match(vod_id):
                return vod_id
        for field in self._bilibili_identity_detail_fields():
            if str(field.label or "").strip().lower() != "season id":
                continue
            for part in field.value_parts:
                action = part.action
                action_value = str(action.value if action is not None else "").strip()
                if _BILIBILI_SS_ID_RE.match(action_value) or _BILIBILI_SEASON_ID_RE.match(action_value):
                    return action_value
                label = str(part.label or "").strip()
                if label.isdigit():
                    return f"season${label}"
            value = str(field.value or "").strip()
            if _BILIBILI_SS_ID_RE.match(value) or _BILIBILI_SEASON_ID_RE.match(value):
                return value
            if value.isdigit():
                return f"season${value}"
        return vod_id

    def _bilibili_season_id_for_binding(self) -> str:
        vod_id = self._metadata_scrape_reset_vod_id()
        for pattern in (_BILIBILI_SS_ID_RE, _BILIBILI_SEASON_ID_RE):
            match = pattern.match(vod_id)
            if match is not None:
                return match.group(1)
        return ""

    def _bilibili_season_binding_key(self) -> tuple[str, str]:
        if not self._is_bilibili_metadata_session():
            return "", ""
        return bilibili_season_binding_title(self._bilibili_season_id_for_binding()), ""

    def _metadata_hydration_vod(self, vod: VodItem) -> VodItem:
        if not self._is_bilibili_metadata_session():
            return vod
        existing_labels = {field.label.strip().lower() for field in vod.detail_fields}
        identity_fields = [
            field
            for field in self._bilibili_identity_detail_fields()
            if field.label.strip().lower() not in existing_labels
        ]
        if not identity_fields:
            return vod
        return replace(vod, detail_fields=[*identity_fields, *vod.detail_fields])

    def _restore_original_metadata_for_reset(self) -> None:
        if self.session is None or self.session.original_vod is None:
            return
        self.session.vod = self._metadata_hydration_vod(self._clone_metadata_snapshot(self.session.original_vod))
        self.session.show_original_metadata = False
        self._reset_metadata_poster_index()
        if 0 <= self.current_index < len(self.session.playlist):
            current_item = self.session.playlist[self.current_index]
            key = self._playlist_identity_key(current_item)
            cached_fields = self.session.original_item_detail_fields_by_key.get(key)
            if cached_fields is not None:
                current_item.detail_fields = deepcopy(cached_fields)

    def _restore_default_metadata_scrape_query(self) -> None:
        if self._metadata_scrape_title_edit is not None:
            self._metadata_scrape_title_edit.setText(self._metadata_scrape_default_title)
        if self._metadata_scrape_year_edit is not None:
            self._metadata_scrape_year_edit.setText(self._metadata_scrape_default_year)
        if self._metadata_scrape_category_combo is not None:
            self._metadata_scrape_category_combo.setCurrentIndex(max(0, self._metadata_scrape_category_combo.findData("")))
        if self._metadata_scrape_provider_combo is not None:
            self._metadata_scrape_provider_combo.setCurrentIndex(max(0, self._metadata_scrape_provider_combo.findData("")))
        self._rerun_metadata_scrape_search()

    def _reset_metadata_scrape_state(self) -> None:
        if self.session is None or self.session.metadata_scrape_service is None:
            return
        self._remember_metadata_scrape_query_state()
        binding_title = self._metadata_scrape_binding_title or str(self.session.vod.vod_name or "").strip()
        # 与保存侧一致：年份取锚点快照，不用当前 vod_year（刮削应用后已被改写）。
        binding_year = str(self._metadata_scrape_binding_year or "").strip()
        if binding_title.startswith("bilibili:season:"):
            binding_year = ""
        bound_provider = ""
        bound_provider_id = ""
        bindings = self.session.metadata_binding_repository
        if bindings is not None and hasattr(bindings, "load"):
            season_binding_title, season_binding_year = self._bilibili_season_binding_key()
            binding = (
                bindings.load(season_binding_title, season_binding_year)
                if season_binding_title
                else None
            )
            if binding is None:
                binding = bindings.load(binding_title, binding_year)
            if binding is not None:
                bound_provider = str(getattr(binding, "provider", "") or "").strip()
                bound_provider_id = str(getattr(binding, "provider_id", "") or "").strip()
        detail_keys: list[tuple[str, str]] = []
        for group in self._metadata_scrape_groups:
            for candidate in getattr(group, "items", []) or []:
                provider = str(getattr(candidate, "provider", "") or "").strip()
                provider_id = str(getattr(candidate, "provider_id", "") or "").strip()
                key = (provider, provider_id)
                if provider and provider_id and key not in detail_keys:
                    detail_keys.append(key)
        reset_title = self._metadata_scrape_default_title
        reset_year = self._metadata_scrape_default_year
        if self._metadata_scrape_title_edit is not None:
            reset_title, reset_year = self._normalize_metadata_scrape_query_inputs(
                self._metadata_scrape_title_edit.text().strip(),
                self._metadata_scrape_year_edit.text().strip() if self._metadata_scrape_year_edit is not None else "",
            )
        self.session.metadata_scrape_service.reset(
            MetadataQuery(
                title=reset_title,
                year=reset_year,
                source_kind=str(getattr(self.session, "source_kind", "") or ""),
                vod_id=self._metadata_scrape_reset_vod_id(),
                type_name=str(self.session.vod.type_name or "").strip(),
                category_name=self._metadata_scrape_selected_category_name(),
            ),
            bound_provider=bound_provider,
            bound_provider_id=bound_provider_id,
            detail_keys=detail_keys,
        )
        cache = MetadataCache(app_cache_dir() / "metadata")
        for namespace in ("tmdb_episode_search", "tmdb_episode_season_detail", "episode_title_playlist"):
            cache.delete_payload_namespace(namespace)
        if bindings is not None and hasattr(bindings, "delete"):
            bindings.delete(binding_title, binding_year)
            season_binding_title, season_binding_year = self._bilibili_season_binding_key()
            if season_binding_title and (season_binding_title, season_binding_year) != (binding_title, binding_year):
                bindings.delete(season_binding_title, season_binding_year)
            if self._metadata_scrape_alias_binding_key:
                alias_title, alias_year = self._metadata_scrape_alias_binding_key
                bindings.delete(alias_title, alias_year)
            self._metadata_scrape_alias_binding_key = None
        # "自动刮削"用手动修正过的查询重跑自动水合：把修正持久化为查询重定向，重开
        # 会话后自动水合先跳到修正标题，否则混淆的网盘目录名每次都从零搜索且搜不到。
        default_title = str(self._metadata_scrape_default_title or "").strip()
        default_year = str(self._metadata_scrape_default_year or "").strip()
        query_corrected = bool(reset_title) and bool(default_title) and (
            reset_title != default_title or (reset_year != default_year and bool(reset_year))
        )
        if (
            query_corrected
            and bindings is not None
            and hasattr(bindings, "save_query_redirect")
            and not binding_title.startswith("bilibili:season:")
        ):
            bindings.save_query_redirect(
                binding_title,
                binding_year,
                reset_title,
                reset_year,
            )
        self._restore_original_metadata_for_reset()
        self._reset_metadata_scrape_search_query()
        self._metadata_hydration_override_title = reset_title
        self._metadata_hydration_override_year = reset_year
        self._metadata_hydration_override_category = self._metadata_scrape_selected_category_name()
        if self.session.metadata_hydrator is not None:
            self._restart_episode_title_after_next_metadata_hydration = True
        self.session.metadata_hydrated = False
        self._start_metadata_hydration()
        if self.session.metadata_hydrator is None and self.session.episode_title_enhancer is not None:
            self.session.episode_titles_hydrated = False
            self._start_episode_title_enhancement()
        self._append_log("已重置元数据缓存与手动绑定，重新开始自动搜索")

    def _selected_metadata_scrape_candidate(self):
        if self._metadata_scrape_result_list is None:
            return None
        current_item = self._metadata_scrape_result_list.currentItem()
        if current_item is None:
            return None
        return current_item.data(Qt.ItemDataRole.UserRole)

    def _apply_selected_metadata_scrape_result(self) -> None:
        if self.session is None or self.session.metadata_scrape_service is None:
            return
        candidate = self._selected_metadata_scrape_candidate()
        if candidate is None:
            return
        self._remember_metadata_scrape_query_state()
        self._metadata_scrape_request_id += 1
        request_id = self._metadata_scrape_request_id
        service = self.session.metadata_scrape_service
        previous_vod = self.session.vod
        playlist_snapshot = list(self.session.playlist)
        selected_category = self._metadata_scrape_selected_category_name()
        bindings = self.session.metadata_binding_repository
        binding_key = None
        alias_binding_key: tuple[str, str] | None = None
        if bindings is not None and hasattr(bindings, "save"):
            binding_title = self._metadata_scrape_binding_title or str(previous_vod.vod_name or "").strip()
            # 年份用打开会话时的锚点快照（即重开时重新解析会返回的年份），不用
            # previous_vod 的当前值——自动水合可能已把 vod_year 改成元数据年份，
            # 保存/查询两侧 key 会错位。
            binding_year = str(self._metadata_scrape_binding_year or "").strip()
            season_binding_title, season_binding_year = self._bilibili_season_binding_key()
            save_title = season_binding_title or binding_title
            save_year = season_binding_year if season_binding_title else binding_year
            binding_key = (save_title, save_year)

        def run() -> None:
            nonlocal alias_binding_key
            try:
                updated_vod = service.apply(previous_vod, candidate)
                if selected_category:
                    updated_vod = replace(updated_vod, category_name=selected_category)
            except Exception as exc:
                if self._is_window_alive():
                    self._metadata_scrape_signals.failed.emit(request_id, f"刮削应用失败: {exc}")
                return
            updated_playlist = None
            episode_title_error = ""
            build_playlist = getattr(service, "build_episode_title_playlist", None)
            if callable(build_playlist):
                try:
                    logger.info(
                        "Metadata scrape apply rebuilding episode titles title=%s year=%s category=%s preferred_provider=%s preferred_id=%s",
                        str(updated_vod.vod_name or "").strip(),
                        str(updated_vod.vod_year or "").strip(),
                        str(updated_vod.category_name or "").strip(),
                        str(getattr(candidate, "provider", "") or "").strip(),
                        str(getattr(candidate, "provider_id", "") or "").strip(),
                        extra={"log_category": "metadata", "log_source": "app"},
                    )
                    parameters = inspect.signature(build_playlist).parameters
                    if "allow_ai_fallback" in parameters:
                        updated_playlist = build_playlist(
                            updated_vod,
                            playlist_snapshot,
                            preferred_candidate=candidate,
                            allow_ai_fallback=False,
                        )
                    else:
                        updated_playlist = build_playlist(
                            updated_vod,
                            playlist_snapshot,
                            preferred_candidate=candidate,
                        )
                except Exception as exc:
                    episode_title_error = str(exc)
            binding_error = ""
            if binding_key is not None and bindings is not None and hasattr(bindings, "save"):
                save_title, save_year = binding_key
                try:
                    logger.info(
                        "Metadata scrape apply saving binding query_title=%s query_year=%s provider=%s provider_id=%s matched_title=%s matched_year=%s",
                        save_title,
                        save_year,
                        str(getattr(candidate, "provider", "") or "").strip(),
                        str(getattr(candidate, "provider_id", "") or "").strip(),
                        str(getattr(candidate, "title", "") or "").strip(),
                        str(getattr(candidate, "year", "") or "").strip(),
                        extra={"log_category": "metadata", "log_source": "app"},
                    )
                    bindings.save(
                        save_title,
                        save_year,
                        provider=getattr(candidate, "provider", ""),
                        provider_id=getattr(candidate, "provider_id", ""),
                        matched_title=getattr(candidate, "title", ""),
                        matched_year=getattr(candidate, "year", ""),
                    )
                except Exception as exc:
                    binding_error = str(exc)
                # 播放中 vod_name 会变成刮削后的标题并写入播放历史，历史重开时
                # media_title 被历史标题覆盖，查询 key 就不再是原始标题了。按刮削后
                # 的标题额外存一条别名绑定，让"按历史重开"也能命中手动刮削结果。
                alias_title = str(updated_vod.vod_name or "").strip()
                alias_differs = (
                    normalize_metadata_binding_title(alias_title)
                    != normalize_metadata_binding_title(save_title)
                )
                if (
                    not binding_error
                    and alias_title
                    and alias_differs
                    and not save_title.startswith("bilibili:season:")
                ):
                    try:
                        bindings.save(
                            alias_title,
                            save_year,
                            provider=getattr(candidate, "provider", ""),
                            provider_id=getattr(candidate, "provider_id", ""),
                            matched_title=getattr(candidate, "title", ""),
                            matched_year=getattr(candidate, "year", ""),
                        )
                        alias_binding_key = (alias_title, save_year)
                    except Exception as exc:
                        logger.warning(
                            "Metadata scrape alias binding save failed title=%s",
                            alias_title,
                            exc_info=exc,
                            extra={"log_category": "metadata", "log_source": "app"},
                        )
            if self._is_window_alive():
                self._metadata_scrape_signals.apply_succeeded.emit(
                    request_id,
                    _MetadataScrapeApplyResult(
                        updated_vod=updated_vod,
                        candidate=candidate,
                        previous_vod=previous_vod,
                        updated_playlist=updated_playlist,
                        binding_key=binding_key,
                        alias_binding_key=alias_binding_key,
                        episode_title_error=episode_title_error,
                        binding_error=binding_error,
                    ),
                )

        threading.Thread(target=run, daemon=True).start()

    def _handle_metadata_scrape_search_succeeded(self, request_id: int, groups) -> None:
        if request_id != self._metadata_scrape_request_id:
            return
        self._populate_metadata_scrape_groups(groups)
        if self._metadata_scrape_group_list is not None:
            self._populate_metadata_scrape_results(self._metadata_scrape_group_list.currentRow())
        if self._metadata_scrape_status_label is not None:
            self._metadata_scrape_status_label.setText("")

    def _handle_metadata_scrape_apply_succeeded(self, request_id: int, result: _MetadataScrapeApplyResult) -> None:
        if request_id != self._metadata_scrape_request_id or self.session is None:
            return
        self._metadata_request_id += 1
        self._pending_metadata_session = None
        candidate = result.candidate
        previous_vod = result.previous_vod
        updated_vod = result.updated_vod
        self.session.vod = updated_vod
        self._reset_metadata_poster_index()
        if 0 <= self.current_index < len(self.session.playlist):
            current_item = self.session.playlist[self.current_index]
            self._snapshot_item_detail_fields(current_item)
            current_item.detail_fields = list(updated_vod.detail_fields)
        if result.episode_title_error:
            self._append_log(f"剧集标题增强失败: {result.episode_title_error}")
        if result.updated_playlist is not None:
            self._episode_title_request_id += 1
            self._pending_episode_title_session = self.session
            self._handle_episode_title_enhancement_succeeded(self._episode_title_request_id, result.updated_playlist)
            if 0 <= self.current_index < len(self.session.playlist):
                self.session.playlist[self.current_index].detail_fields = list(updated_vod.detail_fields)
        if result.binding_error:
            self._append_log(f"刮削绑定保存失败: {result.binding_error}")
        if result.binding_key is not None and not result.binding_error:
            save_title, save_year = result.binding_key
            self._metadata_scrape_binding_title = save_title
            self._metadata_scrape_binding_year = save_year
            self._metadata_scrape_alias_binding_key = result.alias_binding_key
        metadata_log = _build_metadata_update_log(previous_vod, updated_vod)
        self._render_poster()
        self._render_metadata()
        self._render_detail_fields()
        self._refresh_metadata_original_toggle()
        self._refresh_window_title()
        if metadata_log:
            self._append_log(metadata_log)
        self._append_log(f"已绑定手动刮削结果: {candidate.title} ({candidate.provider_label})")
        if self._metadata_scrape_status_label is not None:
            self._metadata_scrape_status_label.setText("")

    def _handle_metadata_scrape_failed(self, request_id: int, message: str) -> None:
        if request_id != self._metadata_scrape_request_id:
            return
        if self._metadata_scrape_status_label is not None:
            self._metadata_scrape_status_label.setText(message)

    def _ensure_danmaku_source_dialog(self) -> QDialog:
        if self._danmaku_source_dialog is not None:
            return self._danmaku_source_dialog
        dialog = _PlayerToolDialog(title="弹幕源", parent=self, size=(760, 480))
        host = dialog.content_widget()
        layout = dialog.content_layout()
        search_row = QGridLayout()
        search_row.setHorizontalSpacing(6)
        search_row.setVerticalSpacing(6)
        search_row.addWidget(QLabel("媒体标题", host), 0, 0)
        self._danmaku_source_title_edit = QLineEdit(host)
        search_row.addWidget(self._danmaku_source_title_edit, 1, 0, alignment=Qt.AlignmentFlag.AlignTop)
        search_row.addWidget(QLabel("集数", host), 0, 1)
        self._danmaku_source_episode_edit = QLineEdit(host)
        search_row.addWidget(self._danmaku_source_episode_edit, 1, 1, alignment=Qt.AlignmentFlag.AlignTop)
        search_row.addWidget(QLabel("搜索来源", host), 0, 2)
        self._danmaku_source_search_provider_combo = FlatComboBox(host)
        for provider_key, provider_label in _DANMAKU_SEARCH_PROVIDER_OPTIONS:
            self._danmaku_source_search_provider_combo.addItem(provider_label, provider_key)
        search_row.addWidget(self._danmaku_source_search_provider_combo, 1, 2, alignment=Qt.AlignmentFlag.AlignTop)
        search_row.setColumnStretch(0, 2)
        search_row.setColumnStretch(1, 1)
        search_row.setColumnStretch(2, 1)
        layout.addLayout(search_row)
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("单集链接", host))
        self._danmaku_source_url_edit = QLineEdit(host)
        self._danmaku_source_url_edit.setPlaceholderText("https://v.qq.com/...")
        url_row.addWidget(self._danmaku_source_url_edit, 1)
        self._danmaku_source_url_download_button = QPushButton("下载", host)
        url_row.addWidget(self._danmaku_source_url_download_button)
        layout.addLayout(url_row)
        columns = QHBoxLayout()
        self._danmaku_source_provider_list = QListWidget(host)
        self._danmaku_source_option_list = QListWidget(host)
        columns.addWidget(self._danmaku_source_provider_list, 1)
        columns.addWidget(self._danmaku_source_option_list, 2)
        layout.addLayout(columns)
        offset_row = QHBoxLayout()
        offset_row.addWidget(QLabel("弹幕偏移", host))
        self._danmaku_source_offset_spin = QDoubleSpinBox(host)
        self._danmaku_source_offset_spin.setRange(-600.0, 600.0)
        self._danmaku_source_offset_spin.setDecimals(1)
        self._danmaku_source_offset_spin.setSingleStep(0.5)
        self._danmaku_source_offset_spin.setSuffix(" 秒")
        self._danmaku_source_offset_spin.setFixedHeight(32)
        offset_row.addWidget(self._danmaku_source_offset_spin)
        self._danmaku_source_offset_reset_button = QPushButton("重置", host)
        offset_row.addWidget(self._danmaku_source_offset_reset_button)
        offset_row.addStretch(1)
        layout.addLayout(offset_row)
        self._danmaku_source_status_label = QLabel("", host)
        layout.addWidget(self._danmaku_source_status_label)
        actions = QHBoxLayout()
        rerun_button = QPushButton("重新搜索", host)
        self._danmaku_source_rerun_button = rerun_button
        reset_button = QPushButton("恢复默认", host)
        clear_button = QPushButton("清除弹幕", host)
        self._danmaku_source_clear_button = clear_button
        switch_button = QPushButton("加载弹幕", host)
        self._danmaku_source_switch_button = switch_button
        rerun_button.clicked.connect(self._rerun_current_item_danmaku_search)
        reset_button.clicked.connect(self._reset_current_item_danmaku_search_query)
        clear_button.clicked.connect(self._clear_current_item_danmaku_source)
        switch_button.clicked.connect(self._switch_current_item_danmaku_source)
        self._danmaku_source_url_download_button.clicked.connect(
            self._download_current_item_danmaku_url
        )
        self._danmaku_source_url_edit.returnPressed.connect(
            self._download_current_item_danmaku_url
        )
        actions.addWidget(rerun_button)
        actions.addWidget(reset_button)
        actions.addWidget(clear_button)
        actions.addWidget(switch_button)
        layout.addLayout(actions)
        self._danmaku_source_provider_list.currentRowChanged.connect(self._handle_danmaku_source_provider_changed)
        self._danmaku_source_search_provider_combo.currentIndexChanged.connect(
            self._handle_danmaku_search_provider_changed
        )
        offset_spin = self._danmaku_source_offset_spin
        offset_spin.valueChanged.connect(self._queue_danmaku_offset_save)
        self._danmaku_source_offset_reset_button.clicked.connect(
            lambda: offset_spin.setValue(0.0)
        )
        self._danmaku_source_dialog = dialog
        self._apply_theme()
        self._refresh_danmaku_source_search_row_heights()
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
        if not current_item.danmaku_search_query_overridden:
            fallback_title = ""
            for candidate in (
                str(current_item.danmaku_search_title or "").strip(),
                str(current_item.media_title or "").strip(),
                str(self.session.vod.vod_name or "").strip() if self.session is not None else "",
            ):
                fallback_title = normalize_metadata_scrape_title(candidate)
                if fallback_title:
                    break
            if fallback_title and current_item.danmaku_search_title != fallback_title:
                current_item.danmaku_search_title = fallback_title
                current_item.danmaku_search_query = " ".join(
                    part for part in (fallback_title, str(current_item.danmaku_search_episode or "").strip()) if part
                ).strip()
        loaded_cached_sources = False
        if (
            not current_item.danmaku_candidates
            and self.session is not None
            and self.session.danmaku_controller is not None
            and hasattr(self.session.danmaku_controller, "load_cached_danmaku_sources")
        ):
            loaded_cached_sources = bool(self.session.danmaku_controller.load_cached_danmaku_sources(current_item))
        if (
            not current_item.danmaku_search_query_overridden
            and not str(current_item.danmaku_search_episode or "").strip()
            and self.session is not None
            and self.session.danmaku_controller is not None
        ):
            # 前置流程可能把集数留空（如网盘替换播放列表）；已有候选时不走缓存加载，
            # 这里兜底重算默认集数，保证搜索带上集数锚点。
            resolve_episode = getattr(
                self.session.danmaku_controller,
                "resolve_default_danmaku_search_episode",
                None,
            )
            if callable(resolve_episode):
                try:
                    episode = str(
                        resolve_episode(current_item, self.session.playlist) or ""
                    ).strip()
                except Exception:
                    episode = ""
                if episode:
                    current_item.danmaku_search_episode = episode
                    search_title = str(current_item.danmaku_search_title or "").strip()
                    if search_title:
                        current_item.danmaku_search_query = " ".join(
                            part for part in (search_title, episode) if part
                        ).strip()
        dialog = self._ensure_danmaku_source_dialog()
        if self._danmaku_source_title_edit is not None:
            self._danmaku_source_title_edit.setText(current_item.danmaku_search_title)
        if self._danmaku_source_episode_edit is not None:
            self._danmaku_source_episode_edit.setText(current_item.danmaku_search_episode)
        self._set_danmaku_search_provider_combo_value(current_item.danmaku_search_provider)
        self._populate_danmaku_source_provider_list(current_item.danmaku_candidates)
        self._populate_danmaku_source_option_list(current_item.danmaku_candidates, current_item.selected_danmaku_provider)
        self._sync_danmaku_offset_controls(current_item)
        self._refresh_danmaku_source_dialog_actions(current_item)
        dialog.show()
        self._refresh_danmaku_source_search_row_heights()
        dialog.raise_()
        dialog.activateWindow()
        has_overridden_query = current_item.danmaku_search_query_overridden and bool(
            str(current_item.danmaku_search_query or "").strip()
        )
        needs_overridden_query_refresh = has_overridden_query and (
            not str(current_item.danmaku_search_title or "").strip()
            or not str(current_item.danmaku_search_episode or "").strip()
        )
        if (
            not loaded_cached_sources
            and not current_item.danmaku_candidates
            and self.session is not None
            and self.session.danmaku_controller is not None
            and hasattr(self.session.danmaku_controller, "refresh_danmaku_sources")
            and (
                (
                    not current_item.danmaku_search_query_overridden
                    and bool(str(current_item.danmaku_search_title or "").strip())
                )
                or needs_overridden_query_refresh
            )
        ):
            current_item.danmaku_status_text = (
                f"搜索中（{self._danmaku_provider_label(current_item.danmaku_search_provider)}）..."
            )
            media_duration_seconds = self._current_media_duration_seconds()
            self._start_danmaku_source_task(
                current_item,
                error_prefix="弹幕源自动搜索失败",
                task=lambda: self.session.danmaku_controller.refresh_danmaku_sources(
                    current_item,
                    query_override=current_item.danmaku_search_query if has_overridden_query else None,
                    search_title_override=None if has_overridden_query else current_item.danmaku_search_title,
                    playlist=self.session.playlist,
                    force_refresh=True,
                    media_duration_seconds=media_duration_seconds,
                    provider_filter=current_item.danmaku_search_provider,
                ),
                debug_label="自动搜索",
            )

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
        if self._current_play_item() is current_item:
            self._sync_danmaku_offset_controls(current_item)
        self._refresh_danmaku_source_dialog_actions(current_item)
        self._refresh_danmaku_source_entry_points()

    def _has_active_danmaku_source_task(self, item: PlayItem | None) -> bool:
        return item is not None and self._active_danmaku_source_task_counts.get(id(item), 0) > 0

    def _load_current_danmaku_offset(self, current_item: PlayItem | None = None) -> float:
        item = current_item or self._current_play_item()
        if item is None:
            return 0.0
        value = 0.0
        session = self.session
        controller = session.danmaku_controller if session is not None else None
        loader = getattr(controller, "load_danmaku_offset", None)
        if session is not None and item.selected_danmaku_provider and callable(loader):
            try:
                value = float(cast(float | int | str, loader(item, session.playlist)))
            except Exception as exc:
                self._append_log(f"弹幕偏移读取失败: {exc}")
                value = 0.0
        value = max(-600.0, min(value, 600.0))
        item.danmaku_offset_seconds = value
        return value

    def _set_danmaku_offset_controls_enabled(self, current_item: PlayItem | None) -> None:
        enabled = bool(
            current_item is not None
            and current_item.danmaku_xml
            and current_item.selected_danmaku_provider
            and not self._has_active_danmaku_source_task(current_item)
        )
        if self._danmaku_source_offset_spin is not None:
            self._danmaku_source_offset_spin.setEnabled(enabled)
        if self._danmaku_source_offset_reset_button is not None:
            self._danmaku_source_offset_reset_button.setEnabled(enabled)

    def _sync_danmaku_offset_controls(self, current_item: PlayItem | None = None) -> None:
        item = current_item or self._current_play_item()
        self._danmaku_offset_save_timer.stop()
        self._pending_danmaku_offset_item = None
        value = self._load_current_danmaku_offset(item)
        if self._danmaku_source_offset_spin is not None:
            self._danmaku_source_offset_spin.blockSignals(True)
            self._danmaku_source_offset_spin.setValue(value)
            self._danmaku_source_offset_spin.blockSignals(False)
        self._set_danmaku_offset_controls_enabled(item)

    def _queue_danmaku_offset_save(self, _value: float) -> None:
        current_item = self._current_play_item()
        if current_item is None:
            return
        self._pending_danmaku_offset_item = current_item
        self._danmaku_offset_save_timer.start()

    def _apply_pending_danmaku_offset(self) -> None:
        current_item = self._current_play_item()
        pending_item = self._pending_danmaku_offset_item
        self._pending_danmaku_offset_item = None
        if (
            current_item is None
            or current_item is not pending_item
            or self._danmaku_source_offset_spin is None
        ):
            return
        value = self._danmaku_source_offset_spin.value()
        current_item.danmaku_offset_seconds = value
        session = self.session
        controller = session.danmaku_controller if session is not None else None
        saver = getattr(controller, "save_danmaku_offset", None)
        if session is not None and callable(saver):
            try:
                saver(current_item, value, session.playlist)
            except Exception as exc:
                self._append_log(f"弹幕偏移保存失败: {exc}")
        if current_item.danmaku_xml:
            self._configure_danmaku_for_current_item()

    def _refresh_danmaku_source_dialog_actions(self, current_item: PlayItem | None) -> None:
        has_active_task = self._has_active_danmaku_source_task(current_item)
        controller = self.session.danmaku_controller if self.session is not None else None
        supports_url_download = callable(
            getattr(controller, "download_danmaku_from_url", None)
        )
        url_download_enabled = bool(
            current_item is not None and not has_active_task and supports_url_download
        )
        if self._danmaku_source_url_edit is not None:
            self._danmaku_source_url_edit.setEnabled(url_download_enabled)
        if self._danmaku_source_url_download_button is not None:
            self._danmaku_source_url_download_button.setEnabled(url_download_enabled)
        if self._danmaku_source_rerun_button is not None:
            self._danmaku_source_rerun_button.setEnabled(current_item is not None)
        if self._danmaku_source_clear_button is not None:
            self._danmaku_source_clear_button.setEnabled(
                bool(
                    current_item is not None
                    and not has_active_task
                    and current_item.danmaku_xml
                )
            )
        if self._danmaku_source_switch_button is not None:
            self._danmaku_source_switch_button.setEnabled(
                bool(
                    current_item is not None
                    and not has_active_task
                    and any(group.options for group in current_item.danmaku_candidates)
                )
            )
        if self._danmaku_source_status_label is not None:
            self._danmaku_source_status_label.setText(current_item.danmaku_status_text if current_item is not None else "")
        self._set_danmaku_offset_controls_enabled(current_item)

    def _start_danmaku_source_task(
        self,
        item: PlayItem,
        *,
        error_prefix: str,
        task: Callable[[], None],
        configure_danmaku_on_success: bool = False,
        debug_label: str = "",
        queue_if_active: bool = False,
        status_error_prefix: str = "",
    ) -> None:
        item_id = id(item)
        active_count = self._active_danmaku_source_task_counts.get(item_id, 0)
        if active_count > 0 and not queue_if_active:
            return
        if active_count <= 0:
            self._danmaku_source_task_pending_state[item_id] = item.danmaku_pending
        self._active_danmaku_source_task_counts[item_id] = active_count + 1
        item.danmaku_pending = True
        self._refresh_danmaku_source_dialog_from_item(item)

        def run() -> None:
            succeeded = False
            failure_status = ""
            try:
                task()
                succeeded = True
            except Exception as exc:
                if status_error_prefix:
                    failure_status = f"{status_error_prefix}: {exc}"
                raise
            finally:
                remaining = self._active_danmaku_source_task_counts.get(item_id, 1) - 1
                if remaining > 0:
                    self._active_danmaku_source_task_counts[item_id] = remaining
                else:
                    self._active_danmaku_source_task_counts.pop(item_id, None)
                    item.danmaku_pending = self._danmaku_source_task_pending_state.pop(item_id, False)
                    item.danmaku_status_text = "" if succeeded else failure_status
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

    def _reset_playback_observation(self) -> None:
        self._observed_media_duration_seconds = 0
        self._clear_chapter_markers()
        self._last_playback_position_seconds = 0
        self._premature_finish_recovery_attempts = 0

    def _update_playback_observation(self, *, position: int, duration: int) -> int:
        if duration > 0:
            self._observed_media_duration_seconds = max(
                self._observed_media_duration_seconds,
                int(duration),
            )
        effective_duration = self._observed_media_duration_seconds
        if (
            position >= 0
            and (effective_duration <= 0 or position <= effective_duration + 2)
            and (position > 0 or self._last_playback_position_seconds <= 0)
        ):
            self._last_playback_position_seconds = int(position)
        return effective_duration if effective_duration > 0 else max(0, int(duration))

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
                playlist=self.session.playlist,
                force_refresh=True,
                media_duration_seconds=media_duration_seconds,
                provider_filter=current_item.danmaku_search_provider,
            ),
            debug_label="重新搜索",
            queue_if_active=True,
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
                playlist=self.session.playlist,
                force_refresh=True,
                media_duration_seconds=media_duration_seconds,
                provider_filter=current_item.danmaku_search_provider,
            ),
            debug_label="恢复默认搜索词",
            queue_if_active=True,
        )

    def _clear_current_item_danmaku_source(self) -> None:
        current_item = self._current_play_item()
        if current_item is None or not current_item.danmaku_xml:
            return
        self._clear_active_danmaku()
        current_item.danmaku_xml = ""
        self._refresh_danmaku_source_dialog_actions(current_item)

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
            debug_label="手动切换",
            queue_if_active=True,
        )

    def _download_current_item_danmaku_url(self) -> None:
        current_item = self._current_play_item()
        session = self.session
        edit = self._danmaku_source_url_edit
        if current_item is None or session is None or edit is None:
            return
        if self._has_active_danmaku_source_task(current_item):
            return
        raw_url = edit.text().strip()
        if not raw_url:
            current_item.danmaku_status_text = "请输入单集链接"
            self._refresh_danmaku_source_dialog_actions(current_item)
            return
        try:
            page_url = normalize_danmaku_episode_url(raw_url)
        except ValueError as exc:
            current_item.danmaku_status_text = str(exc)
            self._refresh_danmaku_source_dialog_actions(current_item)
            return
        download = getattr(session.danmaku_controller, "download_danmaku_from_url", None)
        if not callable(download):
            current_item.danmaku_status_text = "当前弹幕源不支持单集链接下载"
            self._refresh_danmaku_source_dialog_actions(current_item)
            return
        edit.setText(page_url)
        current_item.danmaku_status_text = "下载中（单集链接）..."

        def run_download() -> None:
            download(current_item, page_url)

        self._start_danmaku_source_task(
            current_item,
            error_prefix="单集链接弹幕下载失败",
            task=run_download,
            configure_danmaku_on_success=True,
            debug_label="单集链接下载",
            status_error_prefix="单集链接弹幕下载失败",
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
        auto_action.setChecked(self.audio_combo.currentIndex() == 0)
        auto_action.triggered.connect(lambda: self._set_audio_from_menu("auto", None))
        group.addAction(auto_action)

        for index in range(1, self.audio_combo.count()):
            item_data = self.audio_combo.itemData(index)
            if not isinstance(item_data, tuple) or len(item_data) != 2:
                continue
            mode, track_id = item_data
            action = menu.addAction(self.audio_combo.itemText(index))
            action.setCheckable(True)
            action.setChecked(self.audio_combo.currentIndex() == index)
            action.triggered.connect(
                lambda _checked=False, mode=mode, track_id=track_id: self._set_audio_from_menu(mode, track_id)
            )
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

    def _build_subtitle_delay_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu("字幕延迟", parent)
        group = QActionGroup(menu)
        group.setExclusive(True)
        for label, value in (("提前 0.5秒", -0.5), ("默认", 0.0), ("延后 0.5秒", 0.5)):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(abs(self._subtitle_delay - value) < 1e-6)
            action.triggered.connect(
                lambda _c=False, v=value: self._set_subtitle_delay_from_menu(v)
            )
            group.addAction(action)
        menu.addSeparator()
        menu.addAction("提前 0.1秒", lambda: self._step_subtitle_delay(-0.1))
        menu.addAction("延后 0.1秒", lambda: self._step_subtitle_delay(0.1))
        menu.addAction("重置", lambda: self._set_subtitle_delay_from_menu(0.0))
        return menu

    def _build_audio_delay_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu("音频延迟", parent)
        group = QActionGroup(menu)
        group.setExclusive(True)
        for label, value in (("提前 0.5秒", -0.5), ("默认", 0.0), ("延后 0.5秒", 0.5)):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(abs(self._audio_delay - value) < 1e-6)
            action.triggered.connect(
                lambda _c=False, v=value: self._set_audio_delay_from_menu(v)
            )
            group.addAction(action)
        menu.addSeparator()
        menu.addAction("提前 0.1秒", lambda: self._step_audio_delay(-0.1))
        menu.addAction("延后 0.1秒", lambda: self._step_audio_delay(0.1))
        menu.addAction("重置", lambda: self._set_audio_delay_from_menu(0.0))
        return menu

    def _build_picture_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu("画面调节", parent)
        if not self._picture_adjustment_supported():
            notice = menu.addAction("需启用 hwdec=auto-copy")
            notice.setEnabled(False)
            return menu
        for prop, label in self._PICTURE_ADJUSTMENT_PROPS:
            sub = menu.addMenu(label)
            group = QActionGroup(sub)
            group.setExclusive(True)
            current = self._picture_adjustments.get(prop, 0)
            for preset_label, value in (("减弱", -25), ("默认", 0), ("增强", 25)):
                action = sub.addAction(preset_label)
                action.setCheckable(True)
                action.setChecked(current == value)
                action.triggered.connect(
                    lambda _c=False, p=prop, v=value: self._set_picture_from_menu(p, v)
                )
                group.addAction(action)
            sub.addSeparator()
            sub.addAction("减弱 5", lambda p=prop: self._step_picture(p, -5))
            sub.addAction("增强 5", lambda p=prop: self._step_picture(p, 5))
            sub.addAction("重置", lambda p=prop: self._set_picture_from_menu(p, 0))
        return menu

    def _picture_adjustment_supported(self) -> bool:
        supports = getattr(self.video, "supports_picture_adjustments", None)
        return bool(supports()) if callable(supports) else False

    def _set_subtitle_delay_from_menu(self, seconds: float) -> None:
        clamped = max(-10.0, min(float(seconds), 10.0))
        try:
            self.controls.set_subtitle_delay(clamped)
            self._subtitle_delay = clamped
        except Exception as exc:
            self._append_log(f"字幕延迟设置失败: {exc}")

    def _step_subtitle_delay(self, delta: float) -> None:
        self._set_subtitle_delay_from_menu(self._subtitle_delay + delta)

    def _set_audio_delay_from_menu(self, seconds: float) -> None:
        clamped = max(-10.0, min(float(seconds), 10.0))
        try:
            self.controls.set_audio_delay(clamped)
            self._audio_delay = clamped
        except Exception as exc:
            self._append_log(f"音频延迟设置失败: {exc}")

    def _step_audio_delay(self, delta: float) -> None:
        self._set_audio_delay_from_menu(self._audio_delay + delta)

    def _set_picture_from_menu(self, prop: str, value: int) -> None:
        if not self._picture_adjustment_supported():
            return
        clamped = max(-100, min(int(value), 100))
        try:
            self.controls.set_picture(prop, clamped)
            self._picture_adjustments[prop] = clamped
        except Exception as exc:
            self._append_log(f"画面调节失败: {exc}")

    def _step_picture(self, prop: str, delta: int) -> None:
        current = self._picture_adjustments.get(prop, 0)
        self._set_picture_from_menu(prop, current + delta)

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
            if self.audio_combo.itemData(index) == (mode, track_id):
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
                self._start_external_subtitle_fetch(
                    subtitle,
                    secondary=True,
                    purpose="secondary",
                    previous_track_id=self._secondary_external_subtitle_track_id,
                )
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

    def _start_external_subtitle_fetch(
        self,
        subtitle: ExternalSubtitleOption,
        *,
        secondary: bool,
        purpose: str,
        previous_track_id: int | None = None,
    ) -> None:
        self._external_subtitle_fetch_counter += 1
        token = self._external_subtitle_fetch_counter
        request = _ExternalSubtitleFetchRequest(
            token=token,
            subtitle=subtitle,
            secondary=secondary,
            purpose=purpose,
            previous_track_id=previous_track_id,
        )
        if secondary:
            self._secondary_external_subtitle_fetch = request
        else:
            self._primary_external_subtitle_fetch = request

        def run_fetch() -> None:
            try:
                text = self._fetch_external_subtitle_text(subtitle)
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                self._external_subtitle_fetch_signals.failed.emit(token, subtitle, message)
                return
            self._external_subtitle_fetch_signals.succeeded.emit(token, subtitle, text)

        threading.Thread(target=run_fetch, daemon=True, name="external-subtitle-fetch").start()

    def _take_external_subtitle_fetch_request(
        self,
        token: int,
        subtitle: ExternalSubtitleOption,
    ) -> _ExternalSubtitleFetchRequest | None:
        primary_request = self._primary_external_subtitle_fetch
        if primary_request is not None and primary_request.token == token:
            if primary_request.subtitle is subtitle:
                self._primary_external_subtitle_fetch = None
                return primary_request
        secondary_request = self._secondary_external_subtitle_fetch
        if secondary_request is not None and secondary_request.token == token:
            if secondary_request.subtitle is subtitle:
                self._secondary_external_subtitle_fetch = None
                return secondary_request
        return None

    def _handle_external_subtitle_fetch_succeeded(
        self,
        token: int,
        subtitle: ExternalSubtitleOption,
        text: str,
    ) -> None:
        request = self._take_external_subtitle_fetch_request(token, subtitle)
        if request is None:
            return
        if request.purpose == "primary-manual":
            self._finish_primary_external_subtitle_manual_load(request, text)
        elif request.purpose == "secondary":
            self._finish_secondary_external_subtitle_load(request, text)
        else:
            self._finish_primary_external_subtitle_auto_load(request, text)

    def _handle_external_subtitle_fetch_failed(
        self,
        token: int,
        subtitle: ExternalSubtitleOption,
        message: str,
    ) -> None:
        request = self._take_external_subtitle_fetch_request(token, subtitle)
        if request is None:
            return
        if request.purpose == "secondary":
            self._append_log(f"次字幕切换失败: {message}")
            return
        if request.purpose == "primary-manual":
            self._append_log(f"字幕切换失败: {message}")
            return
        self._append_log(f"字幕切换失败: {message}")
        self._clear_primary_external_subtitle()
        if request.purpose == "primary-retry":
            self._sync_subtitle_combo_for_current_state()

    def _finish_primary_external_subtitle_manual_load(
        self,
        request: _ExternalSubtitleFetchRequest,
        text: str,
    ) -> None:
        subtitle = request.subtitle
        try:
            loaded_track_id, subtitle_path = self._load_external_subtitle_from_text(
                subtitle,
                text,
                secondary=False,
            )
        except Exception as exc:
            self._append_log(f"字幕切换失败: {exc}")
            return
        self._subtitle_preference = SubtitlePreference(mode="external")
        self._primary_external_subtitle_selection = ExternalSubtitleSelection(
            source=subtitle.source,
            option_url=subtitle.url,
            option_name=subtitle.name,
            option_lang=subtitle.lang,
            option_format=subtitle.format,
        )
        self._primary_external_subtitle_track_id = loaded_track_id
        self._primary_external_subtitle_path = subtitle_path
        if request.previous_track_id != loaded_track_id:
            self._remove_external_subtitle_track(request.previous_track_id)
        try:
            self._mark_manual_subtitle_switch_refresh()
            self._apply_primary_external_subtitle_track(loaded_track_id)
        except Exception as exc:
            self._append_log(f"字幕切换失败: {exc}")

    def _finish_primary_external_subtitle_auto_load(
        self,
        request: _ExternalSubtitleFetchRequest,
        text: str,
    ) -> None:
        try:
            loaded_track_id, subtitle_path = self._load_external_subtitle_from_text(
                request.subtitle,
                text,
                secondary=False,
            )
        except Exception as exc:
            if self._should_retry_primary_external_subtitle_apply(exc):
                self._schedule_primary_external_subtitle_retry()
            else:
                self._stop_primary_external_subtitle_retry()
                self._append_log(f"字幕切换失败: {exc}")
            return
        self._primary_external_subtitle_track_id = loaded_track_id
        self._primary_external_subtitle_path = subtitle_path
        if loaded_track_id is None:
            self._schedule_primary_external_subtitle_retry_for_pending_track()
            return
        if not self._apply_primary_external_subtitle_track(loaded_track_id):
            return
        if request.purpose == "primary-retry":
            self._sync_subtitle_combo_for_current_state()
        else:
            self._sync_subtitle_combo_without_tracks()

    def _finish_secondary_external_subtitle_load(
        self,
        request: _ExternalSubtitleFetchRequest,
        text: str,
    ) -> None:
        subtitle = request.subtitle
        try:
            loaded_track_id, subtitle_path = self._load_external_subtitle_from_text(
                subtitle,
                text,
                secondary=True,
            )
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
            if request.previous_track_id != loaded_track_id:
                self._remove_external_subtitle_track(request.previous_track_id)
        except Exception as exc:
            self._append_log(f"次字幕切换失败: {exc}")

    def _load_external_subtitle_from_text(
        self,
        subtitle: ExternalSubtitleOption,
        text: str,
        *,
        secondary: bool,
    ) -> tuple[int | None, Path]:
        if not text.strip():
            raise ValueError("字幕内容为空")
        self._validate_external_subtitle_text(subtitle, text)
        suffix = self._external_subtitle_suffix(subtitle, text)
        subtitle_path = self._write_external_subtitle_file(text, suffix)
        track_id = self.video.load_external_subtitle(str(subtitle_path), select_for_secondary=secondary)
        return track_id, subtitle_path

    def _external_subtitle_suffix(self, subtitle: ExternalSubtitleOption, text: str) -> str:
        url_suffix = Path(urlparse(subtitle.url).path).suffix.lower()
        if url_suffix in {".srt", ".vtt", ".ass", ".ssa"}:
            return url_suffix

        normalized_format = str(subtitle.format or "").strip().lower()
        if normalized_format in {"vtt", "text/vtt"}:
            return ".vtt"
        if normalized_format == "srt" or "subrip" in normalized_format:
            return ".srt"
        if normalized_format in {"ass", "text/x-ass", "application/x-ass"}:
            return ".ass"
        if normalized_format in {"ssa", "text/x-ssa", "application/x-ssa"}:
            return ".ssa"

        stripped_text = text.lstrip()
        if stripped_text.startswith("WEBVTT"):
            return ".vtt"
        if stripped_text.startswith("[Script Info]"):
            return ".ass"
        return ".txt"

    def _validate_external_subtitle_text(self, subtitle: ExternalSubtitleOption, text: str) -> None:
        stripped_text = text.lstrip()
        if subtitle.source == "ytdlp" and "youtube.com/api/timedtext" in subtitle.url and "<html" in stripped_text[:64].lower():
            if "tlang=" in subtitle.url:
                raise ValueError("YouTube 翻译字幕被 Google 拦截，当前无法直接加载")
            raise ValueError("YouTube 字幕请求返回了网页而不是字幕内容")

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

    def _update_volume_value_label(self, value: int) -> None:
        self.volume_value_label.setText(f"{value}%")

    def _change_volume(self, value: int) -> None:
        try:
            self.controls.set_volume(value)
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
            (QKeySequence("S"), self._open_metadata_scrape_dialog),
            (QKeySequence("C"), self._open_subtitle_search_dialog),
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
            (QKeySequence(Qt.Key.Key_Z), lambda: self._step_subtitle_delay(-0.1)),
            (QKeySequence(Qt.Key.Key_X), lambda: self._step_subtitle_delay(0.1)),
            (QKeySequence("Shift+Z"), lambda: self._step_audio_delay(-0.1)),
            (QKeySequence("Shift+X"), lambda: self._step_audio_delay(0.1)),
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
            self.controls.seek(seconds)
            self._mark_recent_user_seek(seconds)
        except Exception as exc:
            self._recent_user_seek_target_seconds = seconds
            if self._current_media_duration_seconds() <= 0:
                self._append_log(f"跳转失败: {exc}")
                self._recover_current_item_after_seek()
                return
            self._append_log(f"跳转失败: {exc}")

    def _mark_recent_user_seek(self, target_seconds: int | None) -> None:
        self._ignore_playback_finished_until = time.monotonic() + 2.0
        self._recent_user_seek_target_seconds = target_seconds

    def _sync_progress_slider(self) -> None:
        if self._slider_dragging:
            return
        duration = self.video.duration_seconds() if hasattr(self.video, "duration_seconds") else 0
        if hasattr(self.video, "position_seconds"):
            try:
                position = self.video.position_seconds() or 0
            except Exception:
                position = 0
        else:
            position = 0
        effective_duration = self._update_playback_observation(
            position=int(position),
            duration=int(duration),
        )
        if (
            not self._auto_advance_locked
            and self.session is not None
            and self.current_index + 1 < len(self.session.playlist)
            and effective_duration > self.opening_spin.value() + self.ending_spin.value()
            and position < effective_duration
            and position + self.ending_spin.value() >= effective_duration
        ):
            logger.info(
                "PlayerWindow auto advance reason=ending index=%s position=%s duration=%s ending=%s",
                self.current_index,
                position,
                effective_duration,
                self.ending_spin.value(),
            )
            self._auto_advance_locked = True
            self.play_next()
            return
        self.progress.setMaximum(max(effective_duration, 0))
        self.progress.setValue(max(min(position, self.progress.maximum()), 0))
        cache_duration = 0
        if hasattr(self.video, "demuxer_cache_duration_seconds"):
            try:
                cache_duration = int(self.video.demuxer_cache_duration_seconds() or 0)
            except Exception:
                cache_duration = 0
        buffer_end = min(int(position) + cache_duration, effective_duration)
        self.progress.set_buffer_value(buffer_end)
        self.current_time_label.setText(self._format_time(position))
        self.duration_label.setText(self._format_time(effective_duration))

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
        self.set_title_bar_visible(not is_fullscreen)
        sidebar_hidden = is_fullscreen or self.wide_button.isChecked()
        metadata_visible = self.toggle_details_button.isChecked()
        poster_visible = self.toggle_poster_button.isChecked()
        log_visible = self.toggle_log_button.isChecked()
        playlist_visible = self._playlist_panel_visible()
        playlist_panel_visible = playlist_visible and not sidebar_hidden
        tree_mode = self._bilibili_grouped_playlist_tree_enabled()
        self._update_log_section_host_layout()
        self.bottom_area.setHidden(is_fullscreen)
        self.sidebar_actions_widget.setHidden(is_fullscreen)
        self.sidebar_container.setHidden(sidebar_hidden)
        if playlist_panel_visible:
            self.playlist_panel.setHidden(False)
            self.sidebar_splitter.setSizes(self._restoreable_sidebar_splitter_sizes())
        else:
            if not self.playlist_panel.isHidden():
                self._remember_sidebar_splitter_sizes()
            self.playlist_panel.setHidden(True)
            self.sidebar_splitter.setSizes([0, 1])
        self.playlist.setHidden(not playlist_visible or tree_mode)
        self.bilibili_playlist_tree.setHidden(not playlist_visible or not tree_mode)
        if playlist_visible and not tree_mode and self.session is not None:
            source_groups = self._session_source_groups()
            active_group = source_groups[self.session.source_group_index] if 0 <= self.session.source_group_index < len(source_groups) else None
            self.playlist_group_combo.setHidden(len(source_groups) <= 1)
            self.playlist_source_combo.setHidden(active_group is None or len(active_group.sources) <= 1)
            active_source = active_group.sources[self.session.source_index] if active_group is not None and active_group.sources else None
            self.playlist_subgroup_combo.setHidden(active_source is None or len(active_source.subgroups) <= 1)
        else:
            self.playlist_group_combo.setHidden(True)
            self.playlist_source_combo.setHidden(True)
            self.playlist_subgroup_combo.setHidden(True)
        self._render_playlist_sort_combo()
        self._render_playlist_title_tabs()
        self.details.setHidden(is_fullscreen or not metadata_visible)
        self.metadata_section.setHidden(is_fullscreen or not metadata_visible)
        self._poster_row_widget.setHidden(is_fullscreen or not metadata_visible or not poster_visible)
        self.log_section.setHidden(is_fullscreen or not log_visible)
        self._refresh_metadata_original_toggle()
        self._refresh_width_adaptive_control_visibility()
        self._update_log_section_max_height()

    def _refresh_width_adaptive_control_visibility(self) -> None:
        if not hasattr(self, "_width_adaptive_control_combos"):
            return
        compact = self.width() <= self._COMPACT_CONTROLS_WIDTH_THRESHOLD
        for combo in self._width_adaptive_control_combos:
            combo.setHidden(compact and not combo.isEnabled())

    def _should_dock_log_to_sidebar_bottom(self) -> bool:
        return (
            not self.isFullScreen()
            and not self.wide_button.isChecked()
            and not self.toggle_details_button.isChecked()
            and self.toggle_log_button.isChecked()
        )

    def _move_log_section_to_layout(self, layout: QVBoxLayout) -> None:
        current_parent = self.log_section.parentWidget()
        current_layout = current_parent.layout() if current_parent is not None else None
        if current_layout is layout:
            return
        if current_layout is not None:
            current_layout.removeWidget(self.log_section)
        if layout is self.details_layout:
            layout.addWidget(self.log_section, 1)
            return
        layout.addWidget(self.log_section)

    def _update_log_section_host_layout(self) -> None:
        if self._should_dock_log_to_sidebar_bottom():
            self._move_log_section_to_layout(self.sidebar_layout)
            return
        self._move_log_section_to_layout(self.details_layout)

    def _update_log_section_max_height(self) -> None:
        details_height = max(self.details.height(), 1)
        max_height = max(details_height // self._DETAIL_LOG_MAX_HEIGHT_DIVISOR, 1)
        self.log_section.setMaximumHeight(max_height)

    def _format_time(self, seconds: int) -> str:
        total_seconds = max(int(seconds), 0)
        minutes, remaining_seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"
        return f"{minutes:02d}:{remaining_seconds:02d}"

    def _format_progress_tooltip(self, seconds: int) -> str:
        text = self._format_time(seconds)
        chapter = self._chapter_at(seconds)
        if chapter is None:
            return text
        return f"{text} · {chapter.label}"

    def _chapter_at(self, seconds: int) -> Chapter | None:
        matched: Chapter | None = None
        for chapter in self._current_chapters:
            if chapter.start_seconds > seconds:
                break
            matched = chapter
        return matched

    def _clear_chapter_markers(self) -> None:
        self._current_chapters = []
        self.progress.set_chapter_positions([])

    def _refresh_chapter_markers(self) -> None:
        if not hasattr(self.video, "chapters"):
            self._clear_chapter_markers()
            return
        try:
            chapters = list(self.video.chapters() or [])
        except Exception:
            chapters = []
        self._current_chapters = chapters
        self.progress.set_chapter_positions(
            chapter.start_seconds for chapter in chapters
        )

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

    def _remember_sidebar_splitter_sizes(self) -> None:
        sizes = self.sidebar_splitter.sizes()
        if self._has_collapsed_splitter_sizes(sizes):
            return
        self._sidebar_splitter_sizes = sizes

    def _restoreable_sidebar_sizes(self) -> list[int]:
        sizes = getattr(self, "_sidebar_sizes", self._DEFAULT_MAIN_SPLITTER_SIZES)
        if self._has_collapsed_splitter_sizes(sizes):
            return self._DEFAULT_MAIN_SPLITTER_SIZES
        return sizes

    def _restoreable_sidebar_splitter_sizes(self) -> list[int]:
        sizes = getattr(self, "_sidebar_splitter_sizes", [1, 1])
        if self._has_collapsed_splitter_sizes(sizes):
            return [1, 1]
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
        if (
            self._pseudo_maximized
            and self._normal_geometry_before_pseudo_maximize is not None
            and self._normal_window_state_before_pseudo_maximize is not None
        ):
            normal = self._normal_geometry_before_pseudo_maximize
            rect_payload = struct.pack(
                ">4i",
                normal.x(),
                normal.y(),
                normal.width(),
                normal.height(),
            )
            self.config.player_window_geometry = (
                self._PSEUDO_MAXIMIZED_GEOMETRY_PREFIX
                + rect_payload
                + self._normal_window_state_before_pseudo_maximize
            )
        else:
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

    def _close_metadata_scrape_dialog(self) -> None:
        self._remember_metadata_scrape_query_state()
        dialog = self._metadata_scrape_dialog
        if dialog is None or not dialog.isVisible():
            return
        dialog.close()

    def _metadata_scrape_cache_binding(self) -> tuple[str, str]:
        binding_title = str(self._metadata_scrape_binding_title or self._metadata_scrape_default_title).strip()
        binding_year = str(self._metadata_scrape_binding_year or self._metadata_scrape_default_year).strip()
        if binding_title:
            return binding_title, binding_year
        if self.session is None:
            return "", ""
        return self._metadata_scrape_current_query()

    def _restore_metadata_scrape_query_state_from_cache(self) -> None:
        binding_title, binding_year = self._metadata_scrape_cache_binding()
        if not binding_title:
            return
        state = load_cached_metadata_scrape_dialog_state(binding_title, binding_year)
        if state is None:
            return
        self._metadata_scrape_saved_title = state.title
        self._metadata_scrape_saved_year = state.year
        self._metadata_scrape_saved_category = state.category
        self._metadata_scrape_saved_provider = ""
        self._metadata_scrape_query_saved = True

    def _remember_metadata_scrape_query_state(self) -> None:
        title = self._metadata_scrape_saved_title
        year = self._metadata_scrape_saved_year
        category = self._metadata_scrape_saved_category
        if self._metadata_scrape_title_edit is not None:
            title = self._metadata_scrape_title_edit.text()
        if self._metadata_scrape_year_edit is not None:
            year = self._metadata_scrape_year_edit.text()
        if self._metadata_scrape_category_combo is not None:
            category = str(self._metadata_scrape_category_combo.currentData() or "")
        if self._metadata_scrape_provider_combo is not None:
            self._metadata_scrape_saved_provider = str(self._metadata_scrape_provider_combo.currentData() or "")
        self._metadata_scrape_saved_title = title
        self._metadata_scrape_saved_year = year
        self._metadata_scrape_saved_category = category
        self._metadata_scrape_query_saved = True
        binding_title, binding_year = self._metadata_scrape_cache_binding()
        if not binding_title:
            return
        save_cached_metadata_scrape_dialog_state(
            binding_title,
            binding_year,
            MetadataScrapeDialogState(title=title, year=year, category=category),
        )

    def _dismiss_escape_dialog(self) -> bool:
        dialog = self._danmaku_settings_dialog
        if dialog is not None and dialog.isVisible():
            self._close_danmaku_settings_dialog()
            return True
        dialog = self._danmaku_source_dialog
        if dialog is not None and dialog.isVisible():
            self._close_danmaku_source_dialog()
            return True
        dialog = self._metadata_scrape_dialog
        if dialog is not None and dialog.isVisible():
            self._close_metadata_scrape_dialog()
            return True
        if self.help_dialog is not None and self.help_dialog.isVisible():
            self._close_help_dialog()
            return True
        return False

    def _return_to_main(self) -> None:
        self._close_help_dialog()
        self._close_danmaku_source_dialog()
        self._close_danmaku_settings_dialog()
        self._close_metadata_scrape_dialog()
        self._close_video_context_menu()
        self._remember_restore_state()
        try:
            self.controls.pause()
        except Exception:
            pass
        self.is_playing = False
        self._sync_native_always_on_top(failure_message="播放时置顶同步失败")
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
        if sys.platform.startswith("win") or (
            self.config is not None and _render_profile_requires_shutdown(getattr(self.config, "mpv_render_profile", "auto"))
        ):
            self.video_widget.shutdown()
        else:
            stop_media = getattr(self.video_widget, "stop_media", None)
            if callable(stop_media):
                stop_media()
        self.hide()
        self.closed_to_main.emit()

    def close(self) -> bool:
        # Returning to the main window keeps this player instance alive; only
        # clear the owner reference when the window is actually closing.
        if (
            not self._quit_requested
            and not self._app_quit_requested
            and self.session is None
        ):
            on_window_closed = getattr(self, "_on_window_closed", None)
            if callable(on_window_closed):
                on_window_closed()
        return super().close()

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
        self._sync_native_always_on_top(failure_message="播放时置顶同步失败")
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
        self._playback_intent_generation += 1
        if self.is_playing:
            self.controls.pause()
        else:
            self.controls.resume()
        self.is_playing = not self.is_playing
        self._sync_native_always_on_top(failure_message="播放时置顶同步失败")
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

    def _playback_finished_is_premature(self) -> bool:
        duration = self._observed_media_duration_seconds
        if duration <= 0:
            return False
        position = self._last_playback_position_seconds
        ending_seconds = self.ending_spin.value() if hasattr(self, "ending_spin") else 0
        end_margin = max(2, int(ending_seconds or 0))
        return position + end_margin < duration

    def _playback_finished_without_progress(self) -> bool:
        # mpv 报告正常 EOF，但首帧从未渲染且播放进度从未离开 0：
        # 播放地址失效或内容不可解码（如加密流），重载同一地址没有意义。
        if self._startup_state.stage is PlaybackStartupStage.PLAYING:
            return False
        return self._last_playback_position_seconds <= 0

    def _handle_playback_finished(self) -> None:
        if self.session is None:
            return
        recent_seek_action = self._recent_seek_playback_finished_action()
        if recent_seek_action == "reload":
            self._recover_current_item_after_seek()
            return
        if recent_seek_action == "ignore":
            return
        if self._playback_finished_without_progress():
            self._handle_playback_finished_without_progress()
            return
        if self._playback_finished_is_premature():
            self._recover_current_item_after_premature_finish()
            return
        logger.info(
            "PlayerWindow auto advance reason=eof-near-end index=%s position=%s duration=%s",
            self.current_index,
            self._last_playback_position_seconds,
            self._observed_media_duration_seconds,
        )
        if self.current_index + 1 >= len(self.session.playlist):
            if self._play_next_drive_directory():
                return
            self.report_progress(force_remote_report=True)
            self._stop_current_playback()
            self._mark_playback_stopped()
            return
        self.play_next()

    def _play_next_drive_directory(self) -> bool:
        """Continue autoplay with the first file in the next drive directory."""
        session = self.session
        if session is None or not getattr(session, "drive_resource_id", ""):
            return False
        source_groups = self._session_source_groups()
        # Resolved drive resources nested under an existing plugin source use the
        # subgroup selector (for example 百度/夸克 -> 第一季/第二季).
        if 0 <= session.source_group_index < len(source_groups):
            parent_group = source_groups[session.source_group_index]
            if 0 <= session.source_index < len(parent_group.sources):
                source = parent_group.sources[session.source_index]
                for subgroup_index in range(source.subgroup_index + 1, len(source.subgroups)):
                    subgroup = source.subgroups[subgroup_index]
                    self._ensure_drive_subgroup_loaded(subgroup)
                    if not subgroup.sources or not subgroup.sources[0].playlist:
                        continue
                    source.subgroup_index = subgroup_index
                    self._switch_active_playlist(
                        subgroup.sources[0].playlist,
                        subgroup.label,
                        target_index=0,
                    )
                    self._render_playlist_source_combos()
                    return True

        # Also support drive resources represented directly as top-level groups.
        for group_index in range(session.source_group_index + 1, len(source_groups)):
            self._ensure_drive_group_loaded(group_index)
            group = source_groups[group_index]
            for source_index, source in enumerate(group.sources):
                if source.playlist:
                    self._switch_active_source(group_index, source_index, target_index=0)
                    return True
        return False

    def _recent_seek_playback_finished_action(self) -> str:
        if time.monotonic() >= self._ignore_playback_finished_until:
            return "handle"
        if not hasattr(self.video, "duration_seconds") or not hasattr(self.video, "position_seconds"):
            return "ignore"
        try:
            duration = int(self.video.duration_seconds() or 0)
            if self._recent_user_seek_target_seconds is not None:
                position = int(self._recent_user_seek_target_seconds)
            else:
                position = int(self.video.position_seconds() or 0)
        except Exception:
            return "ignore"
        if duration <= 0:
            return "reload" if self._recent_user_seek_target_seconds is not None else "ignore"
        ending_seconds = self.ending_spin.value() if hasattr(self, "ending_spin") else 0
        end_margin = max(2, int(ending_seconds or 0))
        return "ignore" if position + end_margin < duration else "handle"

    def _recover_current_item_after_seek(self) -> None:
        if self.session is None or not (0 <= self.current_index < len(self.session.playlist)):
            return
        start_position_seconds = max(0, int(self._recent_user_seek_target_seconds or 0))
        self._ignore_playback_finished_until = 0.0
        self._recent_user_seek_target_seconds = None
        self._append_log(f"正在恢复播放进度: {self._format_time(start_position_seconds)}")
        try:
            self._start_current_item_playback(
                start_position_seconds=start_position_seconds,
                pause=not self.is_playing,
            )
        except Exception as exc:
            self._append_log(f"播放恢复失败: {exc}")

    def _recover_current_item_after_premature_finish(self) -> None:
        position = max(0, int(self._last_playback_position_seconds))
        duration = max(0, int(self._observed_media_duration_seconds))
        if self._premature_finish_recovery_attempts > 0:
            self._stop_after_premature_finish_failure(
                "播放提前结束，恢复失败: "
                f"index={self.current_index} position={position} duration={duration}"
            )
            return
        self._premature_finish_recovery_attempts += 1
        logger.warning(
            "PlayerWindow premature EOF recovery index=%s position=%s duration=%s",
            self.current_index,
            position,
            duration,
        )
        self._append_log(
            f"播放提前结束，正在恢复: {self._format_time(position)} / {self._format_time(duration)}"
        )
        try:
            self._start_current_item_playback(
                start_position_seconds=position,
                pause=not self.is_playing,
            )
        except Exception as exc:
            self._stop_after_premature_finish_failure(
                f"播放提前结束，恢复失败: {exc}"
            )

    def _stop_after_premature_finish_failure(self, message: str) -> None:
        logger.warning("PlayerWindow %s", message)
        self._append_log(message)
        self.is_playing = False
        self._sync_native_always_on_top(failure_message="播放时置顶同步失败")
        self._set_last_player_paused(True)
        self._update_play_button_icon()
        self._refresh_window_title()
        self._stop_current_playback()

    def _handle_playback_finished_without_progress(self) -> None:
        """EOF 时没有任何解码进度：按源不可用处理（自动换线或失败提示）。

        与“提前结束恢复”不同：重载同一地址无法救活失效/加密的源，
        且 duration 未知时也不应误判为“已播完”而自动切集。
        """
        position = max(0, int(self._last_playback_position_seconds))
        duration = max(0, int(self._observed_media_duration_seconds))
        logger.warning(
            "PlayerWindow playback finished without progress index=%s position=%s duration=%s",
            self.current_index,
            position,
            duration,
        )
        if self._try_auto_switch_source_after_failure():
            return
        self._show_failed_startup_state("播放失败: 无法解码任何内容，当前线路可能已失效或加密")
        self._mark_playback_stopped()
        self._append_log(
            "播放失败: 无法解码任何内容（线路可能已失效或加密）: "
            f"index={self.current_index} position={position} duration={duration}"
        )
        self._video_surface_ready = False
        pixmap = self.video_poster_overlay.pixmap()
        if pixmap is not None and not pixmap.isNull():
            self._show_video_poster_overlay(pixmap)

    def _schedule_always_on_top_reapply(self) -> None:
        if (
            self._always_on_top_reapply_pending
            or not self._should_apply_always_on_top()
            or self.isMinimized()
        ):
            return
        self._always_on_top_reapply_pending = True
        QTimer.singleShot(0, self._run_scheduled_always_on_top_reapply)

    def _run_scheduled_always_on_top_reapply(self) -> None:
        self._always_on_top_reapply_pending = False
        self._reapply_always_on_top_after_show()

    def _reapply_always_on_top_after_show(self) -> None:
        should_apply = self._should_apply_always_on_top()
        if not should_apply or not self.isVisible():
            return
        self._always_on_top_applied = False
        try:
            self._convert_true_maximize_to_pseudo_maximize()
            self._set_native_always_on_top(True)
        except Exception as exc:
            logger.exception("PlayerWindow always-on-top restore failed")
            try:
                self._append_log(f"恢复置顶失败: {exc}")
            except Exception:
                pass
        else:
            self._always_on_top_applied = True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._restore_pseudo_maximized_on_show:
            self._restore_pseudo_maximized_on_show = False
            normal_geometry = self._restored_pseudo_normal_geometry
            self._restored_pseudo_normal_geometry = None
            if self._uses_xcb_pseudo_maximize():
                self._enter_pseudo_maximized(normal_geometry=normal_geometry)
        self._schedule_always_on_top_reapply()

    def _play_clicked_item(self, item: QListWidgetItem) -> None:
        row = self.playlist.row(item)
        if row == self.current_index or self.session is None:
            return
        logger.info(
            "PlayerWindow playlist click current_index=%s target_index=%s current_title=%s target_title=%s",
            self.current_index,
            row,
            self.session.playlist[self.current_index].title if 0 <= self.current_index < len(self.session.playlist) else "",
            self.session.playlist[row].title if 0 <= row < len(self.session.playlist) else "",
        )
        self.report_progress(force_remote_report=True)
        self._stop_current_playback()
        try:
            self._play_item_at_index(row, preserve_primary_external_subtitle_selection=True)
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._quit_requested and not self._app_quit_requested and self.session is not None:
            self._close_event_returns_to_main = True
            event.ignore()
            try:
                self._return_to_main()
            finally:
                self._close_event_returns_to_main = False
            return
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
            self._uninstall_danmaku_log_handler()
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
        if (
            not self._quit_requested
            and not self._app_quit_requested
            and self.config is not None
        ):
            self.config.last_active_window = "main"
            self._save_config()
            self.closed_to_main.emit()
        super().closeEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._apply_visibility_state()
            self._schedule_always_on_top_reapply()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_width_adaptive_control_visibility()

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if not hasattr(self, "video_widget"):
            if not isinstance(watched, QObject):
                return False
            return super().eventFilter(cast(QObject, watched), event)
        details = getattr(self, "details", None)
        if watched is details and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._update_log_section_max_height()
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
                if event.button() == Qt.MouseButton.LeftButton:
                    self._release_focus_for_video_press()
                    if self._close_video_context_menu():
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
            if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
                self._release_focus_for_video_press()
                if self._close_video_context_menu():
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
        if event.modifiers() & Qt.KeyboardModifier.KeypadModifier:
            key = event.key()
            if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
                self._apply_danmaku_keypad_line_count(int(key - Qt.Key.Key_0))
                event.accept()
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
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            shifted = event.text().lower()
            if shifted == "z":
                self._step_audio_delay(-0.1)
                event.accept()
                return
            if shifted == "x":
                self._step_audio_delay(0.1)
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
        if key_text == "z":
            self._step_subtitle_delay(-0.1)
            event.accept()
            return
        if key_text == "x":
            self._step_subtitle_delay(0.1)
            event.accept()
            return
        super().keyPressEvent(event)
