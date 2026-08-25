from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import threading
import time
from urllib.parse import urlparse, urlunparse

import httpx
from PySide6.QtCore import QObject, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from atv_player import cache_management
from atv_player.ai import AIProviderConfig, OpenAICompatibleClient
from atv_player.controllers.youtube_category_config import (
    load_youtube_category_config,
    parse_youtube_category_config,
)
from atv_player.danmaku.providers.dandan import probe_dandan_server
from atv_player.models import AppConfig
from atv_player.network_proxy import ProxyConfig, ProxyDecider, ProxyRuleError
from atv_player.source_preferences import (
    DANMAKU_SOURCE_PREFERENCES,
    METADATA_SOURCE_PREFERENCES,
    SUBTITLE_SOURCE_PREFERENCES,
)
from atv_player.ui.log_console import LogConsoleWidget
from atv_player.ui.theme import (
    FlatComboBox,
    build_form_combobox_qss,
    build_form_line_edit_qss,
    build_navigation_tabbar_qss,
    build_player_spinbox_qss,
    configure_form_flat_combobox,
    current_tokens,
)
from atv_player.ui.window_chrome import ThemedDialogBase

_TMDB_CUSTOM_ENDPOINT_VALUE = "__custom__"
_TMDB_ENDPOINT_OPTIONS = [
    ("官方 API - https://api.themoviedb.org", "", "https://api.themoviedb.org"),
    ("Worker - https://tmdb.8866033.xyz", "https://tmdb.8866033.xyz", "https://tmdb.8866033.xyz"),
    ("Worker - https://tmdb.swust-oj.workers.dev", "https://tmdb.swust-oj.workers.dev", "https://tmdb.swust-oj.workers.dev"),
    ("Worker - https://tmdb.8866033.workers.dev", "https://tmdb.8866033.workers.dev", "https://tmdb.8866033.workers.dev"),
]
_TMDB_ENDPOINT_PRESET_VALUES = {item[1] for item in _TMDB_ENDPOINT_OPTIONS}


def _normalize_tmdb_proxy_base_url(value: object) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return text
    path = parsed.path.rstrip("/")
    if path == "/3":
        path = ""
    elif path.endswith("/3"):
        path = path[:-2].rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _build_source_checkbox_layout(checkboxes: list[QCheckBox]) -> QGridLayout:
    layout = QGridLayout()
    column_count = 3 if len(checkboxes) > 4 else 2
    for index, checkbox in enumerate(checkboxes):
        row = index // column_count
        column = index % column_count
        layout.addWidget(checkbox, row, column)
    return layout


class _InitialContentSignals(QObject):
    cache_loaded = Signal(object)
    cache_failed = Signal(str)
    logs_loaded = Signal(object)
    logs_failed = Signal(str)
    tmdb_speed_test_finished = Signal(object)
    dandan_test_finished = Signal(object)


class AdvancedSettingsDialog(ThemedDialogBase):
    def __init__(
        self,
        config: AppConfig,
        save_config: Callable[[], None],
        parent: QWidget | None = None,
        apply_theme: Callable[[], None] | None = None,
        app_log_service=None,
        youtube_category_text_loader: Callable[[str], str] | None = None,
        ai_client_factory: Callable[[AIProviderConfig], object] | None = None,
    ) -> None:
        super().__init__(title="高级设置", parent=parent)
        self._config = config
        self._save_config = save_config
        self._apply_application_theme = apply_theme
        self._app_log_service = app_log_service
        self._youtube_category_text_loader = youtube_category_text_loader
        self._initial_deferred_load_scheduled = False
        self._initial_content_signals = _InitialContentSignals(self)
        self._ai_client_factory = ai_client_factory or OpenAICompatibleClient
        self.resize(920, 560)

        self.settings_tabs = QTabWidget()
        self.settings_tabs.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)
        self.appearance_tab = QWidget()
        self.metadata_tab = QWidget()
        self.danmaku_tab = QWidget()
        self.subtitle_tab = QWidget()
        self.ai_tab = QWidget()
        self.network_proxy_tab = QWidget()
        self.playback_tab = QWidget()
        self.youtube_tab = QWidget()
        self.cache_tab = QWidget()
        self.logs_tab = QWidget()
        self.appearance_group = QGroupBox("外观")
        self.homepage_group = QGroupBox("首页模式")
        self.theme_mode_combo = FlatComboBox()
        self.theme_mode_combo.addItem("浅色", "light")
        self.theme_mode_combo.addItem("深色", "dark")
        self.theme_mode_combo.addItem("跟随系统", "system")
        self.home_mode_combo = FlatComboBox()
        self.home_mode_combo.addItem("浏览", "browse")
        self.home_mode_combo.addItem("经典 (TvBox)", "classic")
        self.home_mode_combo.addItem("精简 (搜索)", "simplified")
        self.home_mode_combo.addItem("媒体 (Emby)", "media")
        # self.home_mode_combo.addItem("电视 (直播)", "tv")
        self.theme_hint_label = QLabel("跟随系统会在应用启动时读取当前系统浅深色；播放器播放区保持偏暗。")
        self.theme_hint_label.setWordWrap(True)
        self.metadata_group = QGroupBox("元数据增强配置")
        self.metadata_source_group = QGroupBox("刮削源")
        self.danmaku_source_group = QGroupBox("弹幕源")
        self.dandan_server_group = QGroupBox("弹弹Play 服务器")
        self.danmaku_cleaning_group = QGroupBox("弹幕清洗")
        self.metadata_enabled_checkbox = QCheckBox("启用元数据增强")
        self.episode_title_enhancement_checkbox = QCheckBox("启用剧集标题增强")
        self.metadata_source_checkboxes: dict[str, QCheckBox] = {}
        self.danmaku_source_checkboxes: dict[str, QCheckBox] = {}
        self.subtitle_source_checkboxes: dict[str, QCheckBox] = {}
        self.subtitle_source_group = QGroupBox("字幕站")
        self.subtitle_token_group = QGroupBox("字幕站账号")
        self.subtitle_subdl_api_key_edit = QLineEdit()
        self.subtitle_subdl_api_key_edit.setPlaceholderText(
            "在 subdl.com 账号面板免费获取；留空则不使用 SubDL"
        )
        self.subtitle_subdl_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.subtitle_assrt_token_edit = QLineEdit()
        self.subtitle_assrt_token_edit.setPlaceholderText(
            "在 assrt.net 用户面板获取；留空则不使用射手网"
        )
        self.subtitle_assrt_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.subtitle_opensubtitles_api_key_edit = QLineEdit()
        self.subtitle_opensubtitles_api_key_edit.setPlaceholderText(
            "在 opensubtitles.com 申请；免费账号每天限 5 次下载"
        )
        self.subtitle_opensubtitles_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.subtitle_subsource_api_key_edit = QLineEdit()
        self.subtitle_subsource_api_key_edit.setPlaceholderText(
            "在 subsource.net 注册后于个人资料页生成；留空则不使用 SubSource"
        )
        self.subtitle_subsource_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.dandan_base_url_edit = QLineEdit()
        self.dandan_base_url_edit.setPlaceholderText(
            "http://host:9321 或 http://host:9321/87654321；留空=关闭此源"
        )
        self.dandan_test_button = QPushButton("测试连接")
        self.dandan_test_status_label = QLabel("")
        self.dandan_test_status_label.setWordWrap(True)
        self._dandan_test_running = False
        self.danmaku_blocked_words_edit = QPlainTextEdit()
        self.danmaku_duplicate_window_spinbox = QSpinBox()
        self.danmaku_duplicate_window_spinbox.setRange(0, 60)
        self.danmaku_duplicate_window_spinbox.setSuffix(" 分钟")
        self.danmaku_convert_top_bottom_checkbox = QCheckBox("顶部/底部弹幕转为滚动弹幕")
        self.bangumi_data_danmaku_checkbox = QCheckBox(
            "Bangumi-data 动漫季号消歧（实验，首次启用需下载几 MB 数据集）"
        )
        self.douban_cookie_edit = QPlainTextEdit()
        self.douban_cookie_edit.setPlaceholderText("填写豆瓣 Cookie；留空时跳过豆瓣官方抓取")
        self.tmdb_api_key_edit = QLineEdit()
        self.tmdb_api_key_edit.setPlaceholderText("可选；代理已隐藏 Key 时可留空")
        self.tmdb_endpoint_combo = FlatComboBox()
        for label, value, _speed_base_url in _TMDB_ENDPOINT_OPTIONS:
            self.tmdb_endpoint_combo.addItem(label, value)
        self.tmdb_endpoint_combo.addItem("自定义代理", _TMDB_CUSTOM_ENDPOINT_VALUE)
        self.tmdb_speed_test_button = QPushButton("测速")
        self._tmdb_speed_test_running = False
        self._last_custom_tmdb_proxy_base_url = ""
        self.tmdb_proxy_base_url_edit = QLineEdit()
        self.tmdb_proxy_base_url_edit.setPlaceholderText("例如 https://tmdb.example.com")
        self.bangumi_access_token_edit = QLineEdit()
        self.bangumi_access_token_edit.setPlaceholderText("可选；留空时使用匿名访问")
        self.ai_group = QGroupBox("AI 智能功能")
        self.ai_enabled_checkbox = QCheckBox("启用智能搜索")
        self.ai_metadata_enrichment_checkbox = QCheckBox("AI 增强元数据刮削")
        self.ai_danmaku_enrichment_checkbox = QCheckBox("AI 优化弹幕搜索")
        self.ai_episode_title_rewrite_checkbox = QCheckBox("AI 改写剧集标题")
        self.ai_following_summary_checkbox = QCheckBox("AI 生成追更详情")
        self.ai_base_url_edit = QLineEdit()
        self.ai_base_url_edit.setPlaceholderText("例如 https://api.openai.com/v1")
        self.ai_api_key_edit = QLineEdit()
        self.ai_api_key_edit.setPlaceholderText("填写 API Key")
        self.ai_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_chat_model_combo = FlatComboBox()
        self.ai_chat_model_combo.setEditable(True)
        self.ai_chat_model_combo.setInsertPolicy(FlatComboBox.InsertPolicy.NoInsert)
        self.ai_chat_model_combo.setPlaceholderText("例如 gpt-4o-mini")
        self.ai_load_models_button = QPushButton("拉取模型")
        self.ai_check_connectivity_button = QPushButton("检查连通性")
        self.ai_timeout_edit = QLineEdit()
        self.ai_timeout_edit.setPlaceholderText("5 - 120")
        self.ai_privacy_label = QLabel(
            "启用后，搜索文本会发送到你配置的 AI 服务商；媒体库、播放历史、收藏列表和 API Key 不会随搜索请求发送。"
        )
        self.ai_privacy_label.setWordWrap(True)
        self.network_proxy_group = QGroupBox("网络代理配置")
        self.network_proxy_mode_combo = FlatComboBox()
        self.network_proxy_mode_combo.addItem("直连", "direct")
        self.network_proxy_mode_combo.addItem("系统代理", "system")
        self.network_proxy_mode_combo.addItem("HTTP", "http")
        self.network_proxy_mode_combo.addItem("HTTPS", "https")
        self.network_proxy_mode_combo.addItem("SOCKS5", "socks5")
        self.network_proxy_url_edit = QLineEdit()
        self.network_proxy_url_edit.setPlaceholderText("例如 socks5://user:pass@127.0.0.1:1080")
        self.network_proxy_bypass_rules_edit = QPlainTextEdit()
        self.network_proxy_bypass_rules_edit.setPlaceholderText("一行一条，例如 localhost 或 10.0.0.0/8")
        self.network_proxy_rules_edit = QPlainTextEdit()
        self.network_proxy_rules_edit.setPlaceholderText("留空则代理所有域名；填写后仅匹配域名走代理，如 .google.com")
        self.network_proxy_scope_label = QLabel(
            "覆盖范围：API、元数据、解析源、弹幕、海报、插件下载、HLS 上游请求、yt-dlp"
        )
        self.network_proxy_scope_label.setWordWrap(True)
        self.playback_group = QGroupBox("播放设置")
        self.following_backend_group = QGroupBox("服务端追剧联动")
        self.following_backend_enabled_checkbox = QCheckBox("接入服务端追剧系统(更新即有源可播)")
        self.following_backend_auto_subscribe_checkbox = QCheckBox("本地追更自动在服务端建立订阅")
        self.following_backend_hint_label = QLabel(
            "需要服务器已启用追剧系统且当前账号为管理员/普通用户。开启后追更检查会并入服务端"
            "挂载资源的可播集数,并可通过「继续播放」直达网盘资源;播放进度多端互通。"
        )
        self.following_backend_hint_label.setWordWrap(True)
        self.playback_auto_switch_source_on_failure_checkbox = QCheckBox("播放失败自动切换线路")
        self.bilibili_grouped_playlist_tree_enabled_checkbox = QCheckBox("B站播放列表显示为分组树")
        self.youtube_group = QGroupBox("YouTube")
        self.youtube_category_group = QGroupBox("分类配置")
        self.youtube_cookie_browser_combo = FlatComboBox()
        self.youtube_cookie_browser_combo.addItem("不使用", "")
        self.youtube_cookie_browser_combo.addItem("Chrome", "chrome")
        self.youtube_cookie_browser_combo.addItem("Edge", "edge")
        self.youtube_cookie_browser_combo.addItem("Firefox", "firefox")
        self.youtube_max_height_combo = FlatComboBox()
        self.youtube_max_height_combo.addItem("480p", 480)
        self.youtube_max_height_combo.addItem("720p", 720)
        self.youtube_max_height_combo.addItem("1080p", 1080)
        self.youtube_max_height_combo.addItem("1440p", 1440)
        self.youtube_max_height_combo.addItem("2160p", 2160)
        self.youtube_video_codec_combo = FlatComboBox()
        self.youtube_video_codec_combo.addItem("VP9", "vp9")
        self.youtube_video_codec_combo.addItem("AV1", "av1")
        self.youtube_video_codec_combo.addItem("自动", "auto")
        self.youtube_default_subtitle_combo = FlatComboBox()
        self.youtube_default_subtitle_combo.addItem("默认（无）", "")
        self.youtube_default_subtitle_combo.addItem("简体中文", "zh-CN")
        self.youtube_default_subtitle_combo.addItem("繁体中文（台湾）", "zh-TW")
        self.youtube_default_subtitle_combo.addItem("繁体中文（香港）", "zh-HK")
        self.youtube_default_subtitle_combo.addItem("英文", "en")
        self.youtube_default_audio_combo = FlatComboBox()
        self.youtube_default_audio_combo.addItem("默认", "")
        self.youtube_default_audio_combo.addItem("汉语", "zh")
        self.youtube_default_audio_combo.addItem("英语", "en")
        self.youtube_metadata_language_combo = FlatComboBox()
        self.youtube_metadata_language_combo.addItem("默认", "")
        self.youtube_metadata_language_combo.addItem("简体中文", "zh-CN")
        self.youtube_metadata_language_combo.addItem("繁体中文（台湾）", "zh-TW")
        self.youtube_metadata_language_combo.addItem("繁体中文（香港）", "zh-HK")
        self.youtube_metadata_language_combo.addItem("英文", "en")
        self.youtube_region_combo = FlatComboBox()
        self.youtube_region_combo.addItem("默认", "")
        self.youtube_region_combo.addItem("中国", "CN")
        self.youtube_region_combo.addItem("中国香港", "HK")
        self.youtube_region_combo.addItem("中国台湾", "TW")
        self.youtube_region_combo.addItem("新加坡", "SG")
        self.youtube_region_combo.addItem("美国", "US")
        self.youtube_region_combo.addItem("日本", "JP")
        self.youtube_category_source_combo = FlatComboBox()
        self.youtube_category_source_combo.addItem("内置", "builtin")
        self.youtube_category_source_combo.addItem("远程 URL", "remote")
        self.youtube_category_source_combo.addItem("本地 JSON", "local")
        self.youtube_category_source_edit = QLineEdit()
        self.youtube_category_source_edit.setPlaceholderText(
            "例如 http://192.168.50.60:4567/zx/json/youtube.json"
        )
        self.youtube_category_local_path_edit = QLineEdit()
        self.youtube_category_local_path_edit.setPlaceholderText("选择本地 youtube.json 或 JSONC 文件")
        self.youtube_category_browse_button = QPushButton("选择")
        self.youtube_category_test_button = QPushButton("测试加载")
        self.youtube_category_refresh_button = QPushButton("刷新缓存")
        self.youtube_category_status_label = QLabel("")
        self.youtube_category_status_label.setWordWrap(True)
        self.mpv_cache_size_edit = QLineEdit()
        self.mpv_cache_size_edit.setPlaceholderText("16 - 4096")
        self.mpv_hwdec_mode_combo = FlatComboBox()
        self.mpv_hwdec_mode_combo.addItem("自动（推荐）", "auto")
        self.mpv_hwdec_mode_combo.addItem("兼容模式（OpenGL）", "compat")
        self.mpv_hwdec_mode_combo.addItem("平衡模式（gpu-next）", "balanced")
        self.mpv_hwdec_mode_combo.addItem("Vulkan 模式", "vulkan")
        self.mpv_hwdec_mode_combo.addItem("高画质模式", "quality")
        self.mpv_hwdec_mode_combo.addItem("极限性能模式", "performance")
        self.mpv_hwdec_mode_combo.addItem("硬件解码（copy-back）", "copy-back")
        self.mpv_hwdec_mode_combo.addItem("软解", "software")
        self.mpv_network_timeout_edit = QLineEdit()
        self.mpv_network_timeout_edit.setPlaceholderText("1 - 300")
        self.mpv_default_readahead_edit = QLineEdit()
        self.mpv_default_readahead_edit.setPlaceholderText("1 - 600")
        self.m3u_proxy_segment_prefetch_size_edit = QLineEdit()
        self.m3u_proxy_segment_prefetch_size_edit.setPlaceholderText("0 - 10")
        self.m3u8_ad_filter_mode_combo = FlatComboBox()
        self.m3u8_ad_filter_mode_combo.addItem("智能过滤（推荐）", "smart")
        self.m3u8_ad_filter_mode_combo.addItem("仅URL标记", "markers")
        self.m3u8_ad_filter_mode_combo.addItem("关闭", "off")
        self.mpv_extra_options_edit = QPlainTextEdit()
        self.mpv_extra_options_edit.setPlaceholderText("一行一个 key=value，例如 cache-pause-wait=8")
        self.playback_scope_label = QLabel(
            "说明：Vulkan 和高画质模式需要较新的显卡驱动；黑屏、花屏或不稳定时请切到兼容模式。普通流预读时长只影响普通流；ISO / YouTube / DASH 仍保留内置专用参数。更多 MPV 配置会在最后应用，并可覆盖同名项。"
        )
        self.playback_scope_label.setWordWrap(True)
        self.youtube_scope_label = QLabel(
            "说明：默认画质设为 1080P 及以下时通常启播更快；2K 及以上会按编码偏好选择视频流。语言和地区设置只影响 yt-dlp 的 YouTube 信息提取。"
        )
        self.youtube_scope_label.setWordWrap(True)
        self.log_console = LogConsoleWidget(
            config=config,
            save_config=save_config,
            app_log_service=app_log_service,
            load_on_init=False,
        )
        self._initial_content_signals.cache_loaded.connect(self._apply_cache_summary)
        self._initial_content_signals.cache_failed.connect(self._show_cache_summary_error)
        self._initial_content_signals.logs_loaded.connect(self.log_console.apply_records)
        self._initial_content_signals.logs_failed.connect(self.log_console.set_status_message)
        self._initial_content_signals.tmdb_speed_test_finished.connect(self._apply_tmdb_speed_results)
        self._initial_content_signals.dandan_test_finished.connect(self._apply_dandan_test_result)
        self.logging_enabled_checkbox = self.log_console.logging_enabled_checkbox
        self.cache_group = QGroupBox("缓存管理")
        self.cache_root_label = QLabel("")
        self.cache_root_label.setWordWrap(True)
        self.cache_total_size_label = QLabel("总大小：0 B")
        self.cache_total_files_label = QLabel("文件数量：0")
        self.cache_open_root_button = QPushButton("打开缓存目录")
        self.cache_refresh_button = QPushButton("刷新")
        self.cache_old_days_spinbox = QSpinBox()
        self.cache_old_days_spinbox.setRange(1, 365)
        self.cache_old_days_spinbox.setValue(30)
        self.cache_old_days_spinbox.setSuffix(" 天以前")
        self.cache_clear_old_button = QPushButton("清理旧缓存")
        self.cache_clear_all_button = QPushButton("清空全部")
        self.cache_category_table = QTableWidget(0, 5)
        self.cache_category_table.setHorizontalHeaderLabels(
            ["分类", "路径", "大小", "文件数量", "操作"]
        )
        self.cache_category_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.cache_category_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.cache_category_table.verticalHeader().setVisible(False)
        header = self.cache_category_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.save_button = QPushButton("保存")
        self.cancel_button = QPushButton("取消")

        self.metadata_enabled_checkbox.setChecked(config.metadata_enhancement_enabled)
        self.episode_title_enhancement_checkbox.setChecked(config.episode_title_enhancement_enabled)
        disabled_metadata_sources = set(config.disabled_metadata_provider_ids)
        for source in METADATA_SOURCE_PREFERENCES:
            checkbox = QCheckBox(source.label)
            checkbox.setChecked(source.id not in disabled_metadata_sources)
            self.metadata_source_checkboxes[source.id] = checkbox
        disabled_danmaku_sources = set(config.disabled_danmaku_provider_ids)
        for source in DANMAKU_SOURCE_PREFERENCES:
            checkbox = QCheckBox(source.label)
            checkbox.setChecked(source.id not in disabled_danmaku_sources)
            self.danmaku_source_checkboxes[source.id] = checkbox
        disabled_subtitle_sources = set(config.disabled_subtitle_provider_ids)
        for source in SUBTITLE_SOURCE_PREFERENCES:
            checkbox = QCheckBox(source.label)
            checkbox.setChecked(source.id not in disabled_subtitle_sources)
            self.subtitle_source_checkboxes[source.id] = checkbox
        self.subtitle_subdl_api_key_edit.setText(config.subtitle_subdl_api_key)
        self.subtitle_assrt_token_edit.setText(config.subtitle_assrt_token)
        self.subtitle_subsource_api_key_edit.setText(config.subtitle_subsource_api_key)
        self.subtitle_opensubtitles_api_key_edit.setText(
            config.subtitle_opensubtitles_api_key
        )
        self.danmaku_blocked_words_edit.setPlainText("\n".join(config.danmaku_blocked_words))
        self.danmaku_duplicate_window_spinbox.setValue(config.danmaku_duplicate_window_minutes)
        self.danmaku_convert_top_bottom_checkbox.setChecked(config.danmaku_convert_top_bottom_to_scroll)
        self.bangumi_data_danmaku_checkbox.setChecked(config.bangumi_data_danmaku_enabled)
        self.dandan_base_url_edit.setText(config.dandan_base_url)
        self.theme_mode_combo.setCurrentIndex(max(0, self.theme_mode_combo.findData(config.theme_mode)))
        self.home_mode_combo.setCurrentIndex(max(0, self.home_mode_combo.findData(config.home_mode)))
        self.douban_cookie_edit.setPlainText(config.metadata_douban_cookie)
        self.tmdb_api_key_edit.setText(config.metadata_tmdb_api_key)
        self._select_tmdb_endpoint(config.metadata_tmdb_proxy_base_url)
        self.bangumi_access_token_edit.setText(config.metadata_bangumi_access_token)
        self.ai_enabled_checkbox.setChecked(config.ai_enabled)
        self.ai_metadata_enrichment_checkbox.setChecked(config.ai_metadata_enrichment_enabled)
        self.ai_danmaku_enrichment_checkbox.setChecked(config.ai_danmaku_enrichment_enabled)
        self.ai_episode_title_rewrite_checkbox.setChecked(config.ai_episode_title_rewrite_enabled)
        self.ai_following_summary_checkbox.setChecked(config.ai_following_summary_enabled)
        self.following_backend_enabled_checkbox.setChecked(config.following_backend_enabled)
        self.following_backend_auto_subscribe_checkbox.setChecked(config.following_backend_auto_subscribe)
        self.ai_base_url_edit.setText(config.ai_base_url)
        self.ai_api_key_edit.setText(config.ai_api_key)
        if config.ai_chat_model:
            self.ai_chat_model_combo.addItem(config.ai_chat_model)
            self.ai_chat_model_combo.setCurrentText(config.ai_chat_model)
        self.ai_timeout_edit.setText(str(config.ai_request_timeout_seconds))
        self.network_proxy_mode_combo.setCurrentIndex(
            max(0, self.network_proxy_mode_combo.findData(config.network_proxy_mode))
        )
        self.network_proxy_url_edit.setText(config.network_proxy_url)
        self.network_proxy_bypass_rules_edit.setPlainText("\n".join(config.network_proxy_bypass_rules))
        self.network_proxy_rules_edit.setPlainText("\n".join(config.network_proxy_rules))
        self.youtube_cookie_browser_combo.setCurrentIndex(
            max(0, self.youtube_cookie_browser_combo.findData(config.youtube_cookie_browser))
        )
        youtube_max_height = config.youtube_max_height if config.youtube_max_height in {480, 720, 1080, 1440, 2160} else 1080
        self.youtube_max_height_combo.setCurrentIndex(
            max(0, self.youtube_max_height_combo.findData(youtube_max_height))
        )
        self.youtube_video_codec_combo.setCurrentIndex(
            max(0, self.youtube_video_codec_combo.findData(config.youtube_video_codec))
        )
        self.youtube_default_subtitle_combo.setCurrentIndex(
            max(0, self.youtube_default_subtitle_combo.findData(config.youtube_default_subtitle_lang))
        )
        self.youtube_default_audio_combo.setCurrentIndex(
            max(0, self.youtube_default_audio_combo.findData(config.youtube_default_audio_lang))
        )
        self.youtube_metadata_language_combo.setCurrentIndex(
            max(0, self.youtube_metadata_language_combo.findData(config.youtube_metadata_language))
        )
        self.youtube_region_combo.setCurrentIndex(
            max(0, self.youtube_region_combo.findData(config.youtube_region))
        )
        self.youtube_category_source_combo.setCurrentIndex(
            max(0, self.youtube_category_source_combo.findData(config.youtube_category_source_type))
        )
        if config.youtube_category_source_type == "local":
            self.youtube_category_local_path_edit.setText(config.youtube_category_source_value)
        else:
            self.youtube_category_source_edit.setText(config.youtube_category_source_value)
        self._sync_youtube_category_source_inputs()
        self._refresh_youtube_category_status_label()
        self.playback_auto_switch_source_on_failure_checkbox.setChecked(
            config.playback_auto_switch_source_on_failure
        )
        self.bilibili_grouped_playlist_tree_enabled_checkbox.setChecked(
            config.bilibili_grouped_playlist_tree_enabled
        )
        self.mpv_cache_size_edit.setText(str(config.mpv_cache_size_mb))
        self.mpv_hwdec_mode_combo.setCurrentIndex(
            max(0, self.mpv_hwdec_mode_combo.findData(config.mpv_render_profile))
        )
        self.mpv_network_timeout_edit.setText(str(config.mpv_network_timeout_seconds))
        self.mpv_default_readahead_edit.setText(str(config.mpv_default_readahead_secs))
        self.m3u_proxy_segment_prefetch_size_edit.setText(str(config.m3u_proxy_segment_prefetch_size))
        self.m3u8_ad_filter_mode_combo.setCurrentIndex(
            max(0, self.m3u8_ad_filter_mode_combo.findData(config.m3u8_ad_filter_mode))
        )
        self.mpv_extra_options_edit.setPlainText(config.mpv_extra_options)

        appearance_layout = QFormLayout()
        appearance_layout.addRow("界面主题", self.theme_mode_combo)
        appearance_layout.addRow("说明", self.theme_hint_label)
        self.appearance_group.setLayout(appearance_layout)
        homepage_layout = QFormLayout()
        homepage_layout.addRow("模式", self.home_mode_combo)
        self.homepage_group.setLayout(homepage_layout)
        appearance_tab_layout = QVBoxLayout(self.appearance_tab)
        appearance_tab_layout.addWidget(self.appearance_group)
        appearance_tab_layout.addWidget(self.homepage_group)
        appearance_tab_layout.addStretch(1)

        metadata_layout = QFormLayout()
        metadata_layout.addRow(self.metadata_enabled_checkbox)
        metadata_layout.addRow(self.episode_title_enhancement_checkbox)
        metadata_layout.addRow("TMDB API Key", self.tmdb_api_key_edit)
        tmdb_endpoint_row = QHBoxLayout()
        tmdb_endpoint_row.addWidget(self.tmdb_endpoint_combo, 1)
        tmdb_endpoint_row.addWidget(self.tmdb_speed_test_button)
        metadata_layout.addRow("TMDB 接入", tmdb_endpoint_row)
        metadata_layout.addRow("TMDB 代理地址", self.tmdb_proxy_base_url_edit)
        metadata_layout.addRow("Bangumi Access Token", self.bangumi_access_token_edit)
        metadata_layout.addRow("豆瓣 Cookie", self.douban_cookie_edit)
        self.metadata_group.setLayout(metadata_layout)
        self.metadata_source_group.setLayout(
            _build_source_checkbox_layout(list(self.metadata_source_checkboxes.values()))
        )
        self.danmaku_source_group.setLayout(
            _build_source_checkbox_layout(list(self.danmaku_source_checkboxes.values()))
        )
        dandan_server_layout = QFormLayout()
        dandan_url_row = QHBoxLayout()
        dandan_url_row.addWidget(self.dandan_base_url_edit, 1)
        dandan_url_row.addWidget(self.dandan_test_button)
        dandan_server_layout.addRow("服务器地址", dandan_url_row)
        dandan_server_layout.addRow("状态", self.dandan_test_status_label)
        self.dandan_server_group.setLayout(dandan_server_layout)
        danmaku_cleaning_layout = QFormLayout()
        danmaku_cleaning_layout.addRow("屏蔽词（每行一个）", self.danmaku_blocked_words_edit)
        danmaku_cleaning_layout.addRow("重复内容窗口", self.danmaku_duplicate_window_spinbox)
        danmaku_cleaning_layout.addRow(self.danmaku_convert_top_bottom_checkbox)
        danmaku_cleaning_layout.addRow(self.bangumi_data_danmaku_checkbox)
        self.danmaku_cleaning_group.setLayout(danmaku_cleaning_layout)
        metadata_tab_layout = QVBoxLayout(self.metadata_tab)
        metadata_tab_layout.addWidget(self.metadata_group)
        metadata_tab_layout.addWidget(self.metadata_source_group)
        metadata_tab_layout.addStretch(1)

        danmaku_tab_layout = QVBoxLayout(self.danmaku_tab)
        danmaku_tab_layout.addWidget(self.danmaku_source_group)
        danmaku_tab_layout.addWidget(self.dandan_server_group)
        danmaku_tab_layout.addWidget(self.danmaku_cleaning_group)
        danmaku_tab_layout.addStretch(1)

        self.subtitle_source_group.setLayout(
            _build_source_checkbox_layout(list(self.subtitle_source_checkboxes.values()))
        )
        subtitle_token_layout = QFormLayout()
        subtitle_token_layout.addRow("SubDL API Key", self.subtitle_subdl_api_key_edit)
        subtitle_token_layout.addRow("射手网 Token", self.subtitle_assrt_token_edit)
        subtitle_token_layout.addRow(
            "OpenSubtitles API Key", self.subtitle_opensubtitles_api_key_edit
        )
        subtitle_token_layout.addRow(
            "SubSource API Key", self.subtitle_subsource_api_key_edit
        )
        subtitle_hint = QLabel(
            "SubHD 与字幕库无需配置即可使用；填写上面的 Token 后会额外启用对应站点。"
            "播放器中按 C 打开字幕搜索。"
        )
        subtitle_hint.setWordWrap(True)
        subtitle_token_layout.addRow(subtitle_hint)
        self.subtitle_token_group.setLayout(subtitle_token_layout)
        subtitle_tab_layout = QVBoxLayout(self.subtitle_tab)
        subtitle_tab_layout.addWidget(self.subtitle_source_group)
        subtitle_tab_layout.addWidget(self.subtitle_token_group)
        subtitle_tab_layout.addStretch(1)

        ai_layout = QFormLayout()
        ai_layout.addRow(self.ai_enabled_checkbox)
        ai_layout.addRow(self.ai_metadata_enrichment_checkbox)
        ai_layout.addRow(self.ai_danmaku_enrichment_checkbox)
        ai_layout.addRow(self.ai_episode_title_rewrite_checkbox)
        ai_layout.addRow(self.ai_following_summary_checkbox)
        ai_layout.addRow("API 地址", self.ai_base_url_edit)
        ai_layout.addRow("API Key", self.ai_api_key_edit)
        ai_model_row = QHBoxLayout()
        ai_model_row.addWidget(self.ai_chat_model_combo, 1)
        ai_model_row.addWidget(self.ai_load_models_button)
        ai_model_row.addWidget(self.ai_check_connectivity_button)
        ai_layout.addRow("Chat 模型", ai_model_row)
        ai_layout.addRow("请求超时", self.ai_timeout_edit)
        ai_layout.addRow("隐私", self.ai_privacy_label)
        self.ai_group.setLayout(ai_layout)
        ai_tab_layout = QVBoxLayout(self.ai_tab)
        ai_tab_layout.addWidget(self.ai_group)
        ai_tab_layout.addStretch(1)

        network_proxy_layout = QFormLayout()
        network_proxy_layout.addRow("代理模式", self.network_proxy_mode_combo)
        network_proxy_layout.addRow("代理地址", self.network_proxy_url_edit)
        network_proxy_layout.addRow("直连规则", self.network_proxy_bypass_rules_edit)
        network_proxy_layout.addRow("代理规则", self.network_proxy_rules_edit)
        network_proxy_layout.addRow("覆盖范围", self.network_proxy_scope_label)
        self.network_proxy_group.setLayout(network_proxy_layout)
        network_proxy_tab_layout = QVBoxLayout(self.network_proxy_tab)
        network_proxy_tab_layout.addWidget(self.network_proxy_group)
        network_proxy_tab_layout.addStretch(1)

        playback_layout = QFormLayout()
        playback_layout.addRow(self.playback_auto_switch_source_on_failure_checkbox)
        playback_layout.addRow(self.bilibili_grouped_playlist_tree_enabled_checkbox)
        playback_layout.addRow("播放缓存大小（MB）", self.mpv_cache_size_edit)
        playback_layout.addRow("渲染模式", self.mpv_hwdec_mode_combo)
        playback_layout.addRow("网络超时", self.mpv_network_timeout_edit)
        playback_layout.addRow("普通流预读时长", self.mpv_default_readahead_edit)
        playback_layout.addRow("m3u代理分片预取大小", self.m3u_proxy_segment_prefetch_size_edit)
        playback_layout.addRow("m3u8广告过滤", self.m3u8_ad_filter_mode_combo)
        playback_layout.addRow("更多 MPV 配置", self.mpv_extra_options_edit)
        playback_layout.addRow("说明", self.playback_scope_label)
        self.playback_group.setLayout(playback_layout)
        following_backend_layout = QFormLayout()
        following_backend_layout.addRow(self.following_backend_enabled_checkbox)
        following_backend_layout.addRow(self.following_backend_auto_subscribe_checkbox)
        following_backend_layout.addRow(self.following_backend_hint_label)
        self.following_backend_group.setLayout(following_backend_layout)
        playback_tab_layout = QVBoxLayout(self.playback_tab)
        playback_tab_layout.addWidget(self.playback_group)
        playback_tab_layout.addWidget(self.following_backend_group)
        playback_tab_layout.addStretch(1)

        youtube_layout = QFormLayout()
        youtube_layout.addRow("Cookie", self.youtube_cookie_browser_combo)
        youtube_layout.addRow("默认画质", self.youtube_max_height_combo)
        youtube_layout.addRow("2K+ 编码", self.youtube_video_codec_combo)
        youtube_layout.addRow("默认字幕", self.youtube_default_subtitle_combo)
        youtube_layout.addRow("默认音轨", self.youtube_default_audio_combo)
        youtube_layout.addRow("语言设置（元数据提取用）", self.youtube_metadata_language_combo)
        youtube_layout.addRow("地区设置", self.youtube_region_combo)
        youtube_layout.addRow("说明", self.youtube_scope_label)
        self.youtube_group.setLayout(youtube_layout)
        youtube_category_layout = QFormLayout()
        youtube_category_layout.addRow("配置源", self.youtube_category_source_combo)
        youtube_category_layout.addRow("远程地址", self.youtube_category_source_edit)
        local_row = QHBoxLayout()
        local_row.addWidget(self.youtube_category_local_path_edit, 1)
        local_row.addWidget(self.youtube_category_browse_button)
        youtube_category_layout.addRow("本地文件", local_row)
        action_row = QHBoxLayout()
        action_row.addWidget(self.youtube_category_test_button)
        action_row.addWidget(self.youtube_category_refresh_button)
        action_row.addStretch(1)
        youtube_category_layout.addRow("操作", action_row)
        youtube_category_layout.addRow("状态", self.youtube_category_status_label)
        self.youtube_category_group.setLayout(youtube_category_layout)
        youtube_tab_layout = QVBoxLayout(self.youtube_tab)
        youtube_tab_layout.addWidget(self.youtube_group)
        youtube_tab_layout.addWidget(self.youtube_category_group)
        youtube_tab_layout.addStretch(1)

        logs_tab_layout = QVBoxLayout(self.logs_tab)
        logs_tab_layout.addWidget(self.log_console)

        cache_summary_row = QHBoxLayout()
        cache_summary_row.addWidget(self.cache_total_size_label)
        cache_summary_row.addWidget(self.cache_total_files_label)
        cache_summary_row.addStretch(1)
        cache_summary_row.addWidget(self.cache_open_root_button)
        cache_summary_row.addWidget(self.cache_refresh_button)
        cache_summary_row.addWidget(self.cache_old_days_spinbox)
        cache_summary_row.addWidget(self.cache_clear_old_button)
        cache_summary_row.addWidget(self.cache_clear_all_button)
        cache_layout = QVBoxLayout()
        cache_layout.addWidget(self.cache_root_label)
        cache_layout.addLayout(cache_summary_row)
        cache_layout.addWidget(self.cache_category_table)
        self.cache_group.setLayout(cache_layout)
        cache_tab_layout = QVBoxLayout(self.cache_tab)
        cache_tab_layout.addWidget(self.cache_group)

        self.settings_tabs.addTab(self.appearance_tab, "外观")
        self.settings_tabs.addTab(self.playback_tab, "播放设置")
        self.settings_tabs.addTab(self.youtube_tab, "YouTube")
        self.settings_tabs.addTab(self.metadata_tab, "元数据")
        self.settings_tabs.addTab(self.danmaku_tab, "弹幕")
        self.settings_tabs.addTab(self.subtitle_tab, "字幕")
        self.settings_tabs.addTab(self.ai_tab, "AI")
        self.settings_tabs.addTab(self.network_proxy_tab, "网络代理")
        self.settings_tabs.addTab(self.cache_tab, "缓存管理")
        self.settings_tabs.addTab(self.logs_tab, "日志")

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.save_button)
        button_row.addWidget(self.cancel_button)

        layout = self.content_layout()
        layout.addWidget(self.settings_tabs)
        layout.addLayout(button_row)

        self.metadata_enabled_checkbox.toggled.connect(self._sync_metadata_inputs)
        self.tmdb_endpoint_combo.currentIndexChanged.connect(self._sync_tmdb_endpoint_inputs)
        self.tmdb_speed_test_button.clicked.connect(self._test_tmdb_endpoints)
        self.dandan_test_button.clicked.connect(self._test_dandan_server)
        self.network_proxy_mode_combo.currentIndexChanged.connect(self._sync_network_proxy_inputs)
        self.youtube_category_source_combo.currentIndexChanged.connect(self._sync_youtube_category_source_inputs)
        self.youtube_category_browse_button.clicked.connect(self._browse_youtube_category_file)
        self.youtube_category_test_button.clicked.connect(self._test_youtube_category_source)
        self.youtube_category_refresh_button.clicked.connect(self._refresh_youtube_category_cache)
        self.ai_load_models_button.clicked.connect(self._load_ai_models)
        self.ai_check_connectivity_button.clicked.connect(self._check_ai_connectivity)
        self.cache_open_root_button.clicked.connect(self._open_cache_root)
        self.cache_refresh_button.clicked.connect(self._refresh_cache_summary)
        self.cache_clear_old_button.clicked.connect(self._clear_old_cache)
        self.cache_clear_all_button.clicked.connect(self._clear_all_cache)
        self.save_button.clicked.connect(self._save)
        self.cancel_button.clicked.connect(self.reject)
        self._sync_metadata_inputs(self.metadata_enabled_checkbox.isChecked())
        self._sync_tmdb_endpoint_inputs()
        self._sync_network_proxy_inputs()
        self._apply_theme()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._initial_deferred_load_scheduled:
            return
        self._initial_deferred_load_scheduled = True
        QTimer.singleShot(0, self._load_initial_deferred_content)

    def _load_initial_deferred_content(self) -> None:
        if not self.isVisible():
            return
        self.cache_root_label.setText("缓存统计加载中...")
        self.log_console.set_status_message("日志加载中...")
        log_filter = self.log_console.current_filter()
        threading.Thread(
            target=self._load_initial_content_in_background,
            args=(log_filter,),
            daemon=True,
        ).start()

    def _load_initial_content_in_background(self, log_filter) -> None:
        try:
            cache_summary = cache_management.build_cache_summary()
        except OSError as exc:
            self._emit_initial_content_signal(self._initial_content_signals.cache_failed, str(exc))
        else:
            self._emit_initial_content_signal(self._initial_content_signals.cache_loaded, cache_summary)

        if self._app_log_service is None:
            self._emit_initial_content_signal(self._initial_content_signals.logs_failed, "日志服务不可用")
            return
        try:
            records = self._app_log_service.load_records(limit=2000, log_filter=log_filter)
        except Exception as exc:
            self._emit_initial_content_signal(self._initial_content_signals.logs_failed, f"日志读取失败: {exc}")
            return
        self._emit_initial_content_signal(self._initial_content_signals.logs_loaded, records)

    def _emit_initial_content_signal(self, signal, *args) -> None:
        try:
            signal.emit(*args)
        except RuntimeError:
            return

    def _apply_theme(self) -> None:
        tokens = current_tokens()
        self.settings_tabs.tabBar().setStyleSheet(build_navigation_tabbar_qss(tokens))
        combo_qss = build_form_combobox_qss(tokens)
        line_edit_qss = build_form_line_edit_qss(tokens)
        for combo in (
            self.theme_mode_combo,
            self.home_mode_combo,
            self.tmdb_endpoint_combo,
            self.network_proxy_mode_combo,
            self.youtube_cookie_browser_combo,
            self.youtube_max_height_combo,
            self.youtube_video_codec_combo,
            self.youtube_default_subtitle_combo,
            self.youtube_default_audio_combo,
            self.youtube_metadata_language_combo,
            self.youtube_region_combo,
            self.youtube_category_source_combo,
            self.mpv_hwdec_mode_combo,
            self.ai_chat_model_combo,
        ):
            combo.setStyleSheet(combo_qss)
            configure_form_flat_combobox(combo, tokens)
        for edit in (
            self.tmdb_api_key_edit,
            self.tmdb_proxy_base_url_edit,
            self.bangumi_access_token_edit,
            self.ai_base_url_edit,
            self.ai_api_key_edit,
            self.ai_timeout_edit,
            self.network_proxy_url_edit,
            self.youtube_category_source_edit,
            self.youtube_category_local_path_edit,
            self.mpv_cache_size_edit,
            self.mpv_network_timeout_edit,
            self.mpv_default_readahead_edit,
            self.m3u_proxy_segment_prefetch_size_edit,
            self.dandan_base_url_edit,
        ):
            edit.setStyleSheet(line_edit_qss)
            edit.setFixedHeight(42)
        self.danmaku_duplicate_window_spinbox.setStyleSheet(
            build_player_spinbox_qss(tokens, min_height=40)
        )
        self.danmaku_duplicate_window_spinbox.setFixedHeight(42)
        if self.ai_chat_model_combo.lineEdit() is not None:
            self.ai_chat_model_combo.lineEdit().setStyleSheet(line_edit_qss)
        self.log_console.apply_theme()

    def _refresh_cache_summary(self) -> None:
        try:
            summary = cache_management.build_cache_summary()
        except OSError as exc:
            QMessageBox.warning(self, "缓存统计失败", str(exc))
            return
        self._apply_cache_summary(summary)

    def _show_cache_summary_error(self, message: str) -> None:
        self.cache_root_label.setText(f"缓存统计失败：{message}")

    def _apply_cache_summary(self, summary) -> None:
        self.cache_root_label.setText(f"缓存目录：{summary.root}")
        self.cache_total_size_label.setText(
            f"总大小：{cache_management.format_cache_size(summary.total_size_bytes)}"
        )
        self.cache_total_files_label.setText(f"文件数量：{summary.total_file_count}")
        self.cache_category_table.setRowCount(len(summary.categories))
        for row, category in enumerate(summary.categories):
            self.cache_category_table.setItem(row, 0, QTableWidgetItem(category.label))
            self.cache_category_table.setItem(
                row,
                1,
                QTableWidgetItem(category.path_summary),
            )
            self.cache_category_table.setItem(
                row,
                2,
                QTableWidgetItem(cache_management.format_cache_size(category.size_bytes)),
            )
            self.cache_category_table.setItem(
                row,
                3,
                QTableWidgetItem(str(category.file_count)),
            )
            self.cache_category_table.setCellWidget(
                row,
                4,
                self._cache_action_widget(category.id),
            )

    def _cache_action_widget(self, category_id: str) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        open_button = QPushButton("打开")
        clear_button = QPushButton("清空")
        open_button.clicked.connect(
            lambda _checked=False, item_id=category_id: (
                self._open_cache_category(item_id)
            )
        )
        clear_button.clicked.connect(
            lambda _checked=False, item_id=category_id: (
                self._clear_cache_category(item_id)
            )
        )
        layout.addWidget(open_button)
        layout.addWidget(clear_button)
        return widget

    def _open_cache_root(self) -> None:
        try:
            path = cache_management.build_cache_summary().root
            path.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except OSError as exc:
            QMessageBox.warning(self, "打开缓存目录失败", str(exc))

    def _open_cache_category(self, category_id: str) -> None:
        try:
            path = cache_management.category_open_path(category_id)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "打开缓存目录失败", str(exc))

    def _clear_cache_category(self, category_id: str) -> None:
        try:
            summary = cache_management.build_cache_summary()
            category = next(item for item in summary.categories if item.id == category_id)
        except (OSError, StopIteration) as exc:
            QMessageBox.warning(self, "清空缓存失败", str(exc))
            return
        result = QMessageBox.question(
            self,
            "清空缓存",
            f"确认清空{category.label}？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        try:
            cache_management.clear_cache_category(category_id)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "清空缓存失败", str(exc))
            return
        self._refresh_cache_summary()

    def _clear_all_cache(self) -> None:
        result = QMessageBox.question(
            self,
            "清空全部缓存",
            "确认清空全部应用缓存？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        try:
            cache_management.clear_all_cache()
        except OSError as exc:
            QMessageBox.warning(self, "清空缓存失败", str(exc))
            return
        self._refresh_cache_summary()

    def _clear_old_cache(self) -> None:
        days = int(self.cache_old_days_spinbox.value())
        result = QMessageBox.question(
            self,
            "清理旧缓存",
            f"确认删除 {days} 天以前的缓存文件？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        try:
            cleanup_result = cache_management.clear_cache_older_than(days)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "清理旧缓存失败", str(exc))
            return
        self._refresh_cache_summary()
        QMessageBox.information(
            self,
            "清理旧缓存",
            "已删除 "
            f"{cleanup_result.removed_file_count} 个旧缓存文件，释放 "
            f"{cache_management.format_cache_size(cleanup_result.removed_size_bytes)}。",
        )

    def _select_tmdb_endpoint(self, value: object) -> None:
        normalized = _normalize_tmdb_proxy_base_url(value)
        if normalized in _TMDB_ENDPOINT_PRESET_VALUES:
            self.tmdb_endpoint_combo.setCurrentIndex(max(0, self.tmdb_endpoint_combo.findData(normalized)))
            self.tmdb_proxy_base_url_edit.setText(normalized)
            return
        self._last_custom_tmdb_proxy_base_url = normalized
        self.tmdb_endpoint_combo.setCurrentIndex(
            max(0, self.tmdb_endpoint_combo.findData(_TMDB_CUSTOM_ENDPOINT_VALUE))
        )
        self.tmdb_proxy_base_url_edit.setText(normalized)

    def _sync_metadata_inputs(self, enabled: bool) -> None:
        self.episode_title_enhancement_checkbox.setEnabled(enabled)
        self.douban_cookie_edit.setEnabled(enabled)
        self.tmdb_api_key_edit.setEnabled(enabled)
        self.tmdb_endpoint_combo.setEnabled(enabled)
        self.tmdb_speed_test_button.setEnabled(enabled and not self._tmdb_speed_test_running)
        self._sync_tmdb_endpoint_inputs()
        self.bangumi_access_token_edit.setEnabled(enabled)
        self.metadata_source_group.setEnabled(enabled)

    def _sync_tmdb_endpoint_inputs(self) -> None:
        enabled = self.metadata_enabled_checkbox.isChecked()
        data = str(self.tmdb_endpoint_combo.currentData() or "")
        custom = data == _TMDB_CUSTOM_ENDPOINT_VALUE
        if custom:
            if not self.tmdb_proxy_base_url_edit.text().strip() and self._last_custom_tmdb_proxy_base_url:
                self.tmdb_proxy_base_url_edit.setText(self._last_custom_tmdb_proxy_base_url)
            self.tmdb_proxy_base_url_edit.setEnabled(enabled)
            return
        current_custom = _normalize_tmdb_proxy_base_url(self.tmdb_proxy_base_url_edit.text())
        if current_custom and current_custom not in _TMDB_ENDPOINT_PRESET_VALUES:
            self._last_custom_tmdb_proxy_base_url = current_custom
        self.tmdb_proxy_base_url_edit.setText(data)
        self.tmdb_proxy_base_url_edit.setEnabled(False)

    def _current_tmdb_proxy_base_url(self) -> str:
        data = str(self.tmdb_endpoint_combo.currentData() or "")
        if data == _TMDB_CUSTOM_ENDPOINT_VALUE:
            return _normalize_tmdb_proxy_base_url(self.tmdb_proxy_base_url_edit.text())
        return data

    def _test_tmdb_endpoints(self) -> None:
        if self._tmdb_speed_test_running:
            return
        self._tmdb_speed_test_running = True
        self.tmdb_speed_test_button.setEnabled(False)
        self.tmdb_speed_test_button.setText("测速中...")
        api_key = self.tmdb_api_key_edit.text().strip()
        threading.Thread(
            target=self._test_tmdb_endpoints_in_background,
            args=(api_key,),
            daemon=True,
        ).start()

    def _test_dandan_server(self) -> None:
        if self._dandan_test_running:
            return
        base_url = self.dandan_base_url_edit.text().strip()
        if not base_url:
            self.dandan_test_status_label.setText("请先填写服务器地址")
            return
        self._dandan_test_running = True
        self.dandan_test_button.setEnabled(False)
        self.dandan_test_button.setText("测试中...")
        self.dandan_test_status_label.setText("")
        threading.Thread(
            target=self._test_dandan_server_in_background,
            args=(base_url,),
            daemon=True,
        ).start()

    def _test_dandan_server_in_background(self, base_url: str) -> None:
        ok, message = probe_dandan_server(get=httpx.get, base_url=base_url)
        self._emit_initial_content_signal(
            self._initial_content_signals.dandan_test_finished, (ok, message)
        )

    def _apply_dandan_test_result(self, result: tuple[bool, str]) -> None:
        ok, message = result
        self._dandan_test_running = False
        self.dandan_test_button.setEnabled(True)
        self.dandan_test_button.setText("测试连接")
        self.dandan_test_status_label.setText(("✅ " if ok else "❌ ") + message)

    def _test_tmdb_endpoints_in_background(self, api_key: str) -> None:
        results: list[dict[str, object]] = []
        for label, value, speed_base_url in _TMDB_ENDPOINT_OPTIONS:
            started_at = time.perf_counter()
            status_text = ""
            elapsed_ms = 0
            try:
                params = {"language": "zh-CN"}
                if api_key:
                    params["api_key"] = api_key
                response = httpx.get(
                    f"{speed_base_url.rstrip('/')}/3/configuration",
                    params=params,
                    timeout=5.0,
                )
                elapsed_ms = max(0, round((time.perf_counter() - started_at) * 1000))
                status_text = "OK" if response.status_code < 500 else str(response.status_code)
            except Exception as exc:
                elapsed_ms = max(0, round((time.perf_counter() - started_at) * 1000))
                status_text = f"失败: {exc.__class__.__name__}"
            results.append(
                {
                    "label": label,
                    "value": value,
                    "elapsed_ms": elapsed_ms,
                    "status": status_text,
                }
            )
        self._emit_initial_content_signal(self._initial_content_signals.tmdb_speed_test_finished, results)

    def _apply_tmdb_speed_results(self, results: list[dict[str, object]]) -> None:
        self._tmdb_speed_test_running = False
        self.tmdb_speed_test_button.setText("测速")
        for result in results:
            value = str(result.get("value") or "")
            index = self.tmdb_endpoint_combo.findData(value)
            if index < 0:
                continue
            label = str(result.get("label") or self.tmdb_endpoint_combo.itemText(index))
            elapsed_ms = int(result.get("elapsed_ms") or 0)
            status = str(result.get("status") or "")
            suffix = f"{elapsed_ms} ms" if status == "OK" else f"{elapsed_ms} ms, {status}"
            self.tmdb_endpoint_combo.setItemText(index, f"{label} ({suffix})")
        self._sync_metadata_inputs(self.metadata_enabled_checkbox.isChecked())

    def _sync_network_proxy_inputs(self) -> None:
        manual_mode = self.network_proxy_mode_combo.currentData() in {"http", "https", "socks5"}
        has_proxy = self.network_proxy_mode_combo.currentData() not in {"direct"}
        self.network_proxy_url_edit.setEnabled(manual_mode)
        self.network_proxy_rules_edit.setEnabled(has_proxy)

    def _sync_youtube_category_source_inputs(self) -> None:
        source_type = str(self.youtube_category_source_combo.currentData() or "builtin")
        self.youtube_category_source_edit.setEnabled(source_type == "remote")
        self.youtube_category_local_path_edit.setEnabled(source_type == "local")
        self.youtube_category_browse_button.setEnabled(source_type == "local")

    def _refresh_youtube_category_status_label(self) -> None:
        if self._config.youtube_category_cache_error:
            self.youtube_category_status_label.setText(f"上次错误：{self._config.youtube_category_cache_error}")
            return
        if self._config.youtube_category_cache_refreshed_at > 0:
            self.youtube_category_status_label.setText(
                f"上次刷新：{self._config.youtube_category_cache_refreshed_at}"
            )
            return
        self.youtube_category_status_label.setText("使用内置分类")

    def _browse_youtube_category_file(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择 YouTube 分类配置",
            self.youtube_category_local_path_edit.text().strip(),
            "JSON files (*.json *.jsonc);;All files (*)",
        )
        if path:
            self.youtube_category_local_path_edit.setText(path)

    def _validated_youtube_category_values(self) -> tuple[str, str] | None:
        source_type = str(self.youtube_category_source_combo.currentData() or "builtin")
        if source_type not in {"builtin", "remote", "local"}:
            QMessageBox.warning(self, "YouTube 分类配置无效", "配置源无效")
            return None
        if source_type == "remote":
            value = self.youtube_category_source_edit.text().strip()
            if not value.startswith(("http://", "https://")):
                QMessageBox.warning(self, "YouTube 分类配置无效", "远程地址必须以 http:// 或 https:// 开头")
                return None
            return source_type, value
        if source_type == "local":
            value = self.youtube_category_local_path_edit.text().strip()
            if not value:
                QMessageBox.warning(self, "YouTube 分类配置无效", "请选择本地 JSON 文件")
                return None
            return source_type, value
        return source_type, ""

    def _draft_youtube_category_config(self) -> AppConfig | None:
        values = self._validated_youtube_category_values()
        if values is None:
            return None
        source_type, source_value = values
        return AppConfig(
            youtube_category_source_type=source_type,
            youtube_category_source_value=source_value,
            youtube_category_cache_json=self._config.youtube_category_cache_json,
            youtube_category_cache_refreshed_at=self._config.youtube_category_cache_refreshed_at,
            youtube_category_cache_error=self._config.youtube_category_cache_error,
        )

    def _set_youtube_category_status(self, category_count: int, filter_count: int) -> None:
        self.youtube_category_status_label.setText(f"加载成功：{category_count} 个分类，{filter_count} 组筛选")

    def _test_youtube_category_source(self) -> None:
        draft = self._draft_youtube_category_config()
        if draft is None:
            return
        try:
            if draft.youtube_category_source_type == "builtin":
                self.youtube_category_status_label.setText("内置分类将在保存后使用")
                return
            text = (
                self._youtube_category_text_loader(draft.youtube_category_source_value)
                if draft.youtube_category_source_type == "remote" and self._youtube_category_text_loader is not None
                else Path(draft.youtube_category_source_value).read_text(encoding="utf-8")
            )
            parsed = parse_youtube_category_config(text)
            filter_count = sum(len(category.filters) for category in parsed.categories)
            self._set_youtube_category_status(len(parsed.categories), filter_count)
        except Exception as exc:
            self.youtube_category_status_label.setText(f"加载失败：{exc}")

    def _refresh_youtube_category_cache(self) -> None:
        draft = self._draft_youtube_category_config()
        if draft is None:
            return
        self._config.youtube_category_source_type = draft.youtube_category_source_type
        self._config.youtube_category_source_value = draft.youtube_category_source_value
        loaded = load_youtube_category_config(
            self._config,
            text_loader=self._youtube_category_text_loader,
            save_config=self._save_config,
        )
        filter_count = sum(len(category.filters) for category in loaded.categories)
        self._set_youtube_category_status(len(loaded.categories), filter_count)

    def _validated_network_proxy_values(self) -> tuple[str, str, list[str], list[str]] | None:
        mode = str(self.network_proxy_mode_combo.currentData() or "direct")
        proxy_url = self.network_proxy_url_edit.text().strip()
        bypass_rules = [
            line.strip()
            for line in self.network_proxy_bypass_rules_edit.toPlainText().splitlines()
            if line.strip()
        ]
        proxy_rules = [
            line.strip()
            for line in self.network_proxy_rules_edit.toPlainText().splitlines()
            if line.strip()
        ]
        if mode in {"http", "https", "socks5"} and not proxy_url:
            QMessageBox.warning(self, "代理地址无效", "手动代理模式需要填写代理地址")
            return None
        scheme_errors = {
            "http": "HTTP 模式要求 http:// 代理地址",
            "https": "HTTPS 模式要求 https:// 代理地址",
            "socks5": "SOCKS5 模式要求 socks5:// 代理地址",
        }
        expected_prefix = f"{mode}://"
        if mode in scheme_errors and proxy_url and not proxy_url.startswith(expected_prefix):
            QMessageBox.warning(self, "代理地址无效", scheme_errors[mode])
            return None
        try:
            ProxyDecider(ProxyConfig(mode="direct", proxy_url="", bypass_rules=bypass_rules, proxy_rules=proxy_rules))
        except ProxyRuleError as exc:
            QMessageBox.warning(self, "代理规则无效", str(exc))
            return None
        return mode, proxy_url, bypass_rules, proxy_rules

    def _validated_youtube_values(self) -> tuple[str, int, str, str, str, str, str] | None:
        browser = str(self.youtube_cookie_browser_combo.currentData() or "")
        if browser not in {"", "chrome", "edge", "firefox"}:
            QMessageBox.warning(self, "YouTube Cookie 无效", "浏览器来源无效")
            return None
        max_height = self.youtube_max_height_combo.currentData()
        if max_height not in {480, 720, 1080, 1440, 2160}:
            QMessageBox.warning(self, "YouTube 默认画质无效", "YouTube 默认画质选项无效")
            return None
        video_codec = str(self.youtube_video_codec_combo.currentData() or "vp9")
        if video_codec not in {"vp9", "av1", "auto"}:
            QMessageBox.warning(self, "YouTube 编码无效", "编码选项无效")
            return None
        subtitle_lang = str(self.youtube_default_subtitle_combo.currentData() or "")
        if subtitle_lang not in {"", "zh-CN", "zh-TW", "zh-HK", "en"}:
            QMessageBox.warning(self, "YouTube 默认字幕无效", "默认字幕选项无效")
            return None
        audio_lang = str(self.youtube_default_audio_combo.currentData() or "")
        if audio_lang not in {"", "zh", "en"}:
            QMessageBox.warning(self, "YouTube 默认音轨无效", "默认音轨选项无效")
            return None
        metadata_language = str(self.youtube_metadata_language_combo.currentData() or "")
        if metadata_language not in {"", "zh-CN", "zh-TW", "zh-HK", "en"}:
            QMessageBox.warning(self, "YouTube 语言设置无效", "语言设置选项无效")
            return None
        region = str(self.youtube_region_combo.currentData() or "")
        if region not in {"", "CN", "US", "JP", "SG", "HK", "TW"}:
            QMessageBox.warning(self, "YouTube 地区设置无效", "地区设置选项无效")
            return None
        return browser, int(max_height), video_codec, subtitle_lang, audio_lang, metadata_language, region

    def _validated_playback_values(self) -> tuple[bool, bool, int, str, int, int, int, str, str] | None:
        def parse_int(text: str, *, label: str, minimum: int, maximum: int) -> int | None:
            try:
                value = int(text.strip())
            except ValueError:
                QMessageBox.warning(self, f"{label}无效", f"{label}必须是整数")
                return None
            if value < minimum or value > maximum:
                QMessageBox.warning(
                    self,
                    f"{label}无效",
                    f"{label}必须在 {minimum} 到 {maximum} 之间",
                )
                return None
            return value

        cache_size = parse_int(
            self.mpv_cache_size_edit.text(),
            label="播放缓存大小（MB）",
            minimum=16,
            maximum=4096,
        )
        timeout = parse_int(
            self.mpv_network_timeout_edit.text(),
            label="网络超时",
            minimum=1,
            maximum=300,
        )
        readahead = parse_int(
            self.mpv_default_readahead_edit.text(),
            label="普通流预读时长",
            minimum=1,
            maximum=600,
        )
        prefetch_size = parse_int(
            self.m3u_proxy_segment_prefetch_size_edit.text(),
            label="m3u代理分片预取大小",
            minimum=0,
            maximum=10,
        )
        if cache_size is None or timeout is None or readahead is None or prefetch_size is None:
            return None

        normalized_lines: list[str] = []
        for index, raw_line in enumerate(self.mpv_extra_options_edit.toPlainText().splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            if "=" not in line:
                QMessageBox.warning(self, "更多 MPV 配置无效", f"更多 MPV 配置第 {index} 行必须是 key=value 格式")
                return None
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                QMessageBox.warning(self, "更多 MPV 配置无效", f"更多 MPV 配置第 {index} 行的 key 不能为空")
                return None
            normalized_lines.append(f"{key}={value}")

        render_profile = str(self.mpv_hwdec_mode_combo.currentData() or "auto")
        if render_profile not in {
            "auto",
            "compat",
            "balanced",
            "vulkan",
            "quality",
            "performance",
            "copy-back",
            "software",
        }:
            QMessageBox.warning(self, "渲染模式无效", "渲染模式选项无效")
            return None

        return (
            self.playback_auto_switch_source_on_failure_checkbox.isChecked(),
            self.bilibili_grouped_playlist_tree_enabled_checkbox.isChecked(),
            cache_size,
            render_profile,
            timeout,
            readahead,
            prefetch_size,
            self.m3u8_ad_filter_mode_combo.currentData(),
            "\n".join(normalized_lines),
        )

    def _validated_ai_values(self) -> tuple[bool, str, str, str, int] | None:
        enabled = self.ai_enabled_checkbox.isChecked()
        base_url = self.ai_base_url_edit.text().strip()
        api_key = self.ai_api_key_edit.text().strip()
        model = self.ai_chat_model_combo.currentText().strip()
        try:
            timeout = int(self.ai_timeout_edit.text().strip() or "30")
        except ValueError:
            QMessageBox.warning(self, "AI 请求超时无效", "AI 请求超时必须是整数")
            return None
        if timeout < 5 or timeout > 120:
            QMessageBox.warning(self, "AI 请求超时无效", "AI 请求超时必须在 5 到 120 秒之间")
            return None
        if enabled and not base_url:
            QMessageBox.warning(self, "AI API 地址无效", "启用智能搜索需要填写 API 地址")
            return None
        if enabled and not api_key:
            QMessageBox.warning(self, "AI API Key 无效", "启用智能搜索需要填写 API Key")
            return None
        if enabled and not model:
            QMessageBox.warning(self, "AI Chat 模型无效", "启用智能搜索需要填写 Chat 模型")
            return None
        return enabled, base_url, api_key, model, timeout

    def _draft_ai_provider_config(self, *, require_model: bool) -> AIProviderConfig | None:
        base_url = self.ai_base_url_edit.text().strip()
        api_key = self.ai_api_key_edit.text().strip()
        model = self.ai_chat_model_combo.currentText().strip()
        try:
            timeout = int(self.ai_timeout_edit.text().strip() or "30")
        except ValueError:
            QMessageBox.warning(self, "AI 请求超时无效", "AI 请求超时必须是整数")
            return None
        if timeout < 5 or timeout > 120:
            QMessageBox.warning(self, "AI 请求超时无效", "AI 请求超时必须在 5 到 120 秒之间")
            return None
        if not base_url:
            QMessageBox.warning(self, "AI API 地址无效", "请先填写 API 地址")
            return None
        if not api_key:
            QMessageBox.warning(self, "AI API Key 无效", "请先填写 API Key")
            return None
        if require_model and not model:
            QMessageBox.warning(self, "AI Chat 模型无效", "请先填写或选择 Chat 模型")
            return None
        return AIProviderConfig(
            base_url=base_url,
            api_key=api_key,
            chat_model=model,
            timeout_seconds=timeout,
        )

    def _build_ai_settings_client(self, *, require_model: bool):
        provider_config = self._draft_ai_provider_config(require_model=require_model)
        if provider_config is None:
            return None
        return self._ai_client_factory(provider_config)

    def _load_ai_models(self) -> None:
        client = self._build_ai_settings_client(require_model=False)
        if client is None:
            return
        self.ai_load_models_button.setEnabled(False)
        try:
            models = list(getattr(client, "list_models")())
        except Exception as exc:
            QMessageBox.warning(self, "AI 模型列表失败", str(exc))
            return
        finally:
            self.ai_load_models_button.setEnabled(True)
        current_model = self.ai_chat_model_combo.currentText().strip()
        seen: set[str] = set()
        model_items: list[str] = []
        for value in [current_model, *models]:
            text = str(value or "").strip()
            if text and text not in seen:
                seen.add(text)
                model_items.append(text)
        self.ai_chat_model_combo.clear()
        self.ai_chat_model_combo.addItems(model_items)
        if current_model:
            self.ai_chat_model_combo.setCurrentText(current_model)
        QMessageBox.information(self, "AI 模型列表", f"已拉取 {len(models)} 个模型")

    def _check_ai_connectivity(self) -> None:
        client = self._build_ai_settings_client(require_model=True)
        if client is None:
            return
        self.ai_check_connectivity_button.setEnabled(False)
        started_at = time.perf_counter()
        try:
            getattr(client, "check_connectivity")()
        except Exception as exc:
            elapsed_ms = max(0, round((time.perf_counter() - started_at) * 1000))
            QMessageBox.warning(self, "AI 连通性失败", f"{exc}\n用时 {elapsed_ms} ms")
            return
        finally:
            self.ai_check_connectivity_button.setEnabled(True)
        elapsed_ms = max(0, round((time.perf_counter() - started_at) * 1000))
        QMessageBox.information(self, "AI 连通性", f"连接正常，用时 {elapsed_ms} ms")

    def _save(self) -> None:
        proxy_values = self._validated_network_proxy_values()
        if proxy_values is None:
            return
        youtube_values = self._validated_youtube_values()
        if youtube_values is None:
            return
        youtube_category_values = self._validated_youtube_category_values()
        if youtube_category_values is None:
            return
        playback_values = self._validated_playback_values()
        if playback_values is None:
            return
        ai_values = self._validated_ai_values()
        if ai_values is None:
            return
        self._config.theme_mode = str(self.theme_mode_combo.currentData() or "system")
        self._config.home_mode = str(self.home_mode_combo.currentData() or "browse")
        self._config.logging_enabled = self.logging_enabled_checkbox.isChecked()
        self._config.metadata_enhancement_enabled = self.metadata_enabled_checkbox.isChecked()
        self._config.episode_title_enhancement_enabled = self.episode_title_enhancement_checkbox.isChecked()
        self._config.disabled_danmaku_provider_ids = [
            provider_id
            for provider_id, checkbox in self.danmaku_source_checkboxes.items()
            if not checkbox.isChecked()
        ]
        self._config.dandan_base_url = self.dandan_base_url_edit.text().strip()
        self._config.disabled_subtitle_provider_ids = [
            provider_id
            for provider_id, checkbox in self.subtitle_source_checkboxes.items()
            if not checkbox.isChecked()
        ]
        self._config.subtitle_subdl_api_key = (
            self.subtitle_subdl_api_key_edit.text().strip()
        )
        self._config.subtitle_assrt_token = self.subtitle_assrt_token_edit.text().strip()
        self._config.subtitle_opensubtitles_api_key = (
            self.subtitle_opensubtitles_api_key_edit.text().strip()
        )
        self._config.subtitle_subsource_api_key = (
            self.subtitle_subsource_api_key_edit.text().strip()
        )
        self._config.bangumi_data_danmaku_enabled = self.bangumi_data_danmaku_checkbox.isChecked()
        self._config.disabled_metadata_provider_ids = [
            provider_id
            for provider_id, checkbox in self.metadata_source_checkboxes.items()
            if not checkbox.isChecked()
        ]
        words = [line.strip() for line in self.danmaku_blocked_words_edit.toPlainText().splitlines()]
        self._config.danmaku_blocked_words = list(dict.fromkeys(word for word in words if word))
        self._config.danmaku_duplicate_window_minutes = self.danmaku_duplicate_window_spinbox.value()
        self._config.danmaku_convert_top_bottom_to_scroll = self.danmaku_convert_top_bottom_checkbox.isChecked()
        self._config.metadata_douban_cookie = self.douban_cookie_edit.toPlainText().strip()
        self._config.metadata_tmdb_api_key = self.tmdb_api_key_edit.text().strip()
        self._config.metadata_tmdb_proxy_base_url = self._current_tmdb_proxy_base_url()
        self._config.metadata_bangumi_access_token = self.bangumi_access_token_edit.text().strip()
        (
            self._config.ai_enabled,
            self._config.ai_base_url,
            self._config.ai_api_key,
            self._config.ai_chat_model,
            self._config.ai_request_timeout_seconds,
        ) = ai_values
        self._config.ai_metadata_enrichment_enabled = self.ai_metadata_enrichment_checkbox.isChecked()
        self._config.ai_danmaku_enrichment_enabled = self.ai_danmaku_enrichment_checkbox.isChecked()
        self._config.ai_episode_title_rewrite_enabled = self.ai_episode_title_rewrite_checkbox.isChecked()
        self._config.ai_following_summary_enabled = self.ai_following_summary_checkbox.isChecked()
        self._config.following_backend_enabled = self.following_backend_enabled_checkbox.isChecked()
        self._config.following_backend_auto_subscribe = self.following_backend_auto_subscribe_checkbox.isChecked()
        self._config.network_proxy_mode, self._config.network_proxy_url, self._config.network_proxy_bypass_rules, self._config.network_proxy_rules = proxy_values
        (
            self._config.youtube_cookie_browser,
            self._config.youtube_max_height,
            self._config.youtube_video_codec,
            self._config.youtube_default_subtitle_lang,
            self._config.youtube_default_audio_lang,
            self._config.youtube_metadata_language,
            self._config.youtube_region,
        ) = youtube_values
        (
            self._config.youtube_category_source_type,
            self._config.youtube_category_source_value,
        ) = youtube_category_values
        (
            self._config.playback_auto_switch_source_on_failure,
            self._config.bilibili_grouped_playlist_tree_enabled,
            self._config.mpv_cache_size_mb,
            self._config.mpv_render_profile,
            self._config.mpv_network_timeout_seconds,
            self._config.mpv_default_readahead_secs,
            self._config.m3u_proxy_segment_prefetch_size,
            self._config.m3u8_ad_filter_mode,
            self._config.mpv_extra_options,
        ) = playback_values
        self._save_config()
        if self._apply_application_theme is not None:
            self._apply_application_theme()
        self.accept()
