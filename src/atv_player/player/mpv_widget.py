from __future__ import annotations

import ctypes
import ctypes.util
import glob
import logging
import math
import os
import platform
import re
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import QCoreApplication, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QMouseEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from atv_player.models import AppConfig
from atv_player.player.mpv_library import (
    custom_mpv_library_diagnostics,
    prepare_custom_mpv_library,
)
from atv_player.player.mpv_user_config import (
    ShaderPreset,
    discover_shader_presets,
    resolve_mpv_config_dir,
)
from atv_player.player.ytdlp_runtime import (
    resolve_mpv_ytdl_raw_options,
    resolve_mpv_ytdlp_path,
)

_MPV_ERROR_MESSAGES = {
    -1: "事件队列已满",
    -2: "内存分配失败",
    -3: "播放器未初始化",
    -4: "参数无效",
    -5: "选项不存在",
    -6: "选项格式错误",
    -7: "选项值无效",
    -8: "属性不存在",
    -9: "属性格式错误",
    -10: "属性当前不可用",
    -11: "属性访问失败",
    -12: "执行播放器命令失败",
    -13: "媒体加载失败",
    -14: "音频输出初始化失败",
    -15: "视频输出初始化失败",
    -16: "没有可播放的音视频流",
    -17: "无法识别媒体格式",
    -18: "当前系统不支持该操作",
    -19: "功能尚未实现",
    -20: "未指定错误",
}

_DEFAULT_STREAM_PROFILE: dict[str, object] = {
    "cache-pause": "yes",
    "cache-pause-initial": "yes",
    "cache-pause-wait": 3,
    "demuxer-readahead-secs": 20,
}

_ISO_PROXY_STREAM_PROFILE: dict[str, object] = {
    "cache-pause": "no",
    "cache-pause-initial": "no",
    "cache-pause-wait": 0,
    "demuxer-readahead-secs": 3,
}

_LOW_LATENCY_STREAM_PROFILE: dict[str, object] = {
    "cache-pause": "no",
    "cache-pause-initial": "no",
    "cache-pause-wait": 0,
    "demuxer-readahead-secs": 3,
}

# DASH 直连分发(视频 asset + 外挂音频 asset):两条普通媒体文件流,关掉初始
# 缓冲暂停让续播尽快起画面,保留可观前向缓冲防上游 CDN 抖动。
_DASH_DIRECT_STREAM_PROFILE: dict[str, object] = {
    "cache-pause": "no",
    "cache-pause-initial": "no",
    "cache-pause-wait": 0,
    "demuxer-readahead-secs": 30,
}

_YTDL_STREAM_PROFILE: dict[str, object] = {
    "cache-pause": "yes",
    "cache-pause-initial": "yes",
    "cache-pause-wait": 5,
    # "cache-secs": 120,
    "demuxer-readahead-secs": 120,
}

logger = logging.getLogger(__name__)
# 直播弹幕专用的 osd-overlay id(避免与其它 OSD 覆盖层冲突)
_LIVE_DANMAKU_OSD_ID = 4242

# 外挂音轨断粮看门狗:DASH 直连的音频走独立上游,劣化边缘会"挂着轨但不
# 出声"(audio-pts 冻结)。周期采样,窗口内 audio-pts 全程无变化才判死;
# 音频是 A/V 同步主,上游硬断时播放时间会一起冻结,同样要触发重挂。
_AUDIO_STARVATION_POLL_MILLISECONDS = 2000
_AUDIO_STARVATION_WINDOW_SECONDS = 10.0
_AUDIO_STARVATION_MIN_SAMPLES = 5
_AUDIO_STARVATION_COOLDOWN_SECONDS = 30.0
_AUDIO_STARVATION_MAX_RELOADS = 3
# mpv_terminate_destroy 会同步等待全部内部线程退出;ffmpeg demuxer 卡死时永不返回。
# shutdown() 在 GUI 线程被调用,terminate 挪到后台线程执行,
# 超过该时长仍未返回则放弃等待(泄漏实例)。
_MPV_TERMINATE_TIMEOUT_SECONDS = 10.0
_NVIDIA_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)*)")
_LINUX_NVIDIA_DRIVER_MISMATCH: tuple[str, str] | bool | None = None
_WINDOWS_MPV_DIAGNOSTIC_STAGES_LOGGED: set[str] = set()
_WINDOWS_MPV_DLL_NAMES = ("libmpv-2.dll", "mpv-2.dll", "mpv.dll")
_VALID_RENDER_PROFILES = {
    "auto",
    "compat",
    "balanced",
    "vulkan",
    "quality",
    "performance",
    "copy-back",
    "software",
}


def _version_sort_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _extract_nvidia_version(text: str) -> str:
    match = _NVIDIA_VERSION_RE.search(text)
    return match.group(1) if match is not None else ""


def _read_linux_nvidia_kernel_version() -> str:
    try:
        with open("/proc/driver/nvidia/version", encoding="utf-8") as handle:
            return _extract_nvidia_version(handle.read())
    except Exception:
        return ""


def _read_linux_nvidia_userspace_version() -> str:
    versions: set[str] = set()
    for pattern in (
        "/lib*/x86_64-linux-gnu/libnvidia-glcore.so.*",
        "/usr/lib*/x86_64-linux-gnu/libnvidia-glcore.so.*",
        "/lib*/x86_64-linux-gnu/libEGL_nvidia.so.*",
        "/usr/lib*/x86_64-linux-gnu/libEGL_nvidia.so.*",
    ):
        for candidate in glob.glob(pattern):
            version = _extract_nvidia_version(os.path.basename(candidate))
            if version:
                versions.add(version)
    if not versions:
        return ""
    return max(versions, key=_version_sort_key)


def detect_linux_nvidia_driver_mismatch() -> tuple[str, str] | None:
    global _LINUX_NVIDIA_DRIVER_MISMATCH

    if _LINUX_NVIDIA_DRIVER_MISMATCH is False:
        return None
    if isinstance(_LINUX_NVIDIA_DRIVER_MISMATCH, tuple):
        return _LINUX_NVIDIA_DRIVER_MISMATCH
    if not sys.platform.startswith("linux"):
        _LINUX_NVIDIA_DRIVER_MISMATCH = False
        return None
    kernel_version = _read_linux_nvidia_kernel_version()
    userspace_version = _read_linux_nvidia_userspace_version()
    if kernel_version and userspace_version and kernel_version != userspace_version:
        _LINUX_NVIDIA_DRIVER_MISMATCH = (userspace_version, kernel_version)
        return _LINUX_NVIDIA_DRIVER_MISMATCH
    _LINUX_NVIDIA_DRIVER_MISMATCH = False
    return None


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _normalize_render_profile(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _VALID_RENDER_PROFILES else "auto"


def _detect_gpu_vendor() -> str:
    override = str(os.getenv("ATV_GPU_VENDOR") or "").strip().lower()
    if override in {"nvidia", "amd", "intel", "unknown"}:
        return override
    return "unknown"


def _explicit_render_profile_options(profile: str) -> dict[str, object]:
    if profile == "compat":
        return {"vo": "gpu", "hwdec": "auto-safe", "profile": "fast"}
    if profile == "balanced":
        return {"vo": "gpu", "hwdec": "auto-safe"}
    if profile == "vulkan":
        return {"vo": "gpu-next", "gpu_api": "vulkan", "hwdec": "auto-safe"}
    if profile == "quality":
        return {
            "vo": "gpu",
            "hwdec": "auto-safe",
            "scale": "ewa_lanczossharp",
            "cscale": "ewa_lanczossharp",
            "sigmoid_upscaling": "yes",
            "deband": "yes",
        }
    if profile == "performance":
        return {
            "vo": "gpu",
            "hwdec": "auto-safe",
            "profile": "sw-fast",
            "vd_lavc_threads": 1,
            "deband": "no",
            "interpolation": "no",
        }
    if profile == "copy-back":
        return {"vo": "gpu", "hwdec": "auto-copy"}
    if profile == "software":
        return {"vo": "gpu", "hwdec": "no"}
    return {}


def _auto_render_profile_options() -> dict[str, object]:
    vendor = _detect_gpu_vendor()
    if sys.platform.startswith(("linux", "win")):
        if vendor == "nvidia":
            return {"vo": "gpu", "hwdec": "nvdec"}
        if sys.platform.startswith("win") and vendor in {"amd", "intel"}:
            return {"vo": "gpu", "hwdec": "d3d11va"}
        if sys.platform.startswith("linux") and vendor in {"amd", "intel"}:
            return {"vo": "gpu", "hwdec": "auto-safe"}
    return {"vo": "gpu", "hwdec": "auto-safe"}


def _render_profile_options(profile: str) -> dict[str, object]:
    normalized = _normalize_render_profile(profile)
    if normalized == "auto":
        return _auto_render_profile_options()
    return _explicit_render_profile_options(normalized)


def _render_profile_requires_shutdown(profile: str) -> bool:
    return _normalize_render_profile(profile) in {"vulkan", "quality", "performance"}


def _is_vulkan_render_options(options: dict[str, object]) -> bool:
    return options.get("gpu_api") == "vulkan"


def _fallback_render_options(options: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    if not _is_vulkan_render_options(options):
        return []
    return [
        ("balanced", _explicit_render_profile_options("balanced")),
        ("compat", _explicit_render_profile_options("compat")),
    ]


def _existing_windows_mpv_dll_candidates() -> list[str]:
    if not sys.platform.startswith("win"):
        return []
    search_dirs = [
        os.getcwd(),
        os.path.dirname(sys.executable),
        str(os.getenv("MPV_DYLIB_PATH") or ""),
    ]
    search_dirs.extend(str(os.getenv("PATH") or "").split(os.pathsep))
    found: list[str] = []
    for directory in _dedupe_preserve_order(search_dirs):
        for dll_name in _WINDOWS_MPV_DLL_NAMES:
            candidate = Path(directory) / dll_name
            if candidate.is_file():
                found.append(str(candidate))
    return _dedupe_preserve_order(found)


def _loaded_windows_module_path(module_name: str) -> str:
    if not sys.platform.startswith("win"):
        return ""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_module_handle = kernel32.GetModuleHandleW
        get_module_handle.argtypes = [ctypes.c_wchar_p]
        get_module_handle.restype = ctypes.c_void_p
        module_handle = get_module_handle(module_name)
        if not module_handle:
            return ""
        buffer = ctypes.create_unicode_buffer(4096)
        get_module_filename = kernel32.GetModuleFileNameW
        get_module_filename.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
        get_module_filename.restype = ctypes.c_uint32
        length = get_module_filename(ctypes.c_void_p(module_handle), buffer, len(buffer))
        if not length:
            return ""
        return buffer.value[:length]
    except Exception:
        return ""


def _loaded_windows_mpv_dll_paths() -> list[str]:
    loaded = [_loaded_windows_module_path(dll_name) for dll_name in _WINDOWS_MPV_DLL_NAMES]
    return _dedupe_preserve_order([path for path in loaded if path])


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    id: int
    title: str
    lang: str
    is_default: bool
    is_forced: bool
    label: str


@dataclass(frozen=True, slots=True)
class AudioTrack:
    id: int
    title: str
    lang: str
    is_default: bool
    is_forced: bool
    label: str


@dataclass(frozen=True, slots=True)
class Chapter:
    index: int
    title: str
    start_seconds: float
    label: str


class MpvWidget(QWidget):
    double_clicked = Signal()
    left_clicked = Signal()
    playback_finished = Signal()
    playback_failed = Signal(str)
    file_loaded = Signal()
    video_picture_state_changed = Signal(str)
    pause_state_changed = Signal(bool)
    subtitle_tracks_changed = Signal()
    audio_tracks_changed = Signal()
    external_audio_attach_failed = Signal(str)
    external_audio_starved = Signal(str)
    chapters_changed = Signal()
    context_menu_requested = Signal()
    context_menu_dismiss_requested = Signal()
    _gui_call_requested = Signal(object)

    def __init__(self, parent=None, config: AppConfig | None = None) -> None:
        super().__init__(parent)
        self._config = config or AppConfig()
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._player: Any | None = None
        self._video_picture_state = "idle"
        self._video_out_params: dict[str, object] = {}
        self._telemetry: dict[str, object] = {}
        self._telemetry_handlers: list[object] = []
        self._audio_cover_active = False
        self._audio_cover_mode = False
        self._playback_finished_emitted = False
        self._player_property_cache: dict[str, object] = {}
        # loadfile 刚发出就 audio-add(select)会撞上 AO 初始化竞态,mpv 返回 -12
        # 但音轨往往实际已挂上;失败时记录,待 file-loaded 后校验补挂。
        self._pending_external_audio_files = ""
        self._external_audio_files = ""
        self._audio_starvation_timer = QTimer(self)
        self._audio_starvation_timer.setInterval(_AUDIO_STARVATION_POLL_MILLISECONDS)
        self._audio_starvation_timer.timeout.connect(self._check_external_audio_starvation)
        self._audio_starvation_samples: list[tuple[float, tuple[object, object]]] = []
        self._audio_starvation_last_fire = 0.0
        self._audio_starvation_reloads = 0
        self._audio_pts_supported: bool | None = None
        self._placeholder = QLabel("")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout = QVBoxLayout(self)
        layout.addWidget(self._placeholder)
        self._windows_file_loaded_timer = QTimer(self)
        self._windows_file_loaded_timer.setSingleShot(True)
        self._windows_file_loaded_timer.timeout.connect(self.file_loaded.emit)
        self._gui_call_requested.connect(
            self._execute_gui_call,
            Qt.ConnectionType.QueuedConnection,
        )

    def _on_widget_thread(self) -> bool:
        return QThread.currentThread() is self.thread()

    @Slot(object)
    def _execute_gui_call(self, callback: object) -> None:
        if callable(callback):
            callback()

    def _post_to_widget_thread(self, callback) -> None:
        if self._on_widget_thread():
            callback()
            return
        self._gui_call_requested.emit(callback)

    def _run_on_widget_thread(self, callback):
        if self._on_widget_thread():
            return callback()
        result: dict[str, object] = {}
        done = threading.Event()

        def run() -> None:
            try:
                result["value"] = callback()
            except BaseException as exc:  # pragma: no cover - re-raised on caller thread
                result["error"] = exc
            finally:
                done.set()

        self._gui_call_requested.emit(run)
        done.wait()
        error = result.get("error")
        if isinstance(error, BaseException):
            raise error
        return result.get("value")

    def _set_video_picture_state(self, state: str) -> None:
        if self._video_picture_state == state:
            return
        self._video_picture_state = state
        self.video_picture_state_changed.emit(state)

    def _emit_playback_finished_once(self) -> None:
        if self._playback_finished_emitted:
            return
        self._playback_finished_emitted = True
        self.playback_finished.emit()

    def _base_player_options(self) -> dict[str, object]:
        options = dict(
            wid=str(int(self.winId())),
            hwdec="auto-safe",
            force_window="yes",
            audio_spdif="no",
            ad="ffmpeg",
            input_default_bindings=False,
            input_vo_keyboard=False,
            cache=True,
            cache_pause_initial=True,
            cache_pause_wait=3,
            demuxer_max_bytes=f"{int(getattr(self._config, 'mpv_cache_size_mb', 512) or 512)}M",
            demuxer_max_back_bytes="128M",
            demuxer_readahead_secs=int(getattr(self._config, "mpv_default_readahead_secs", 20) or 20),
            stream_buffer_size="4M",
            network_timeout=int(getattr(self._config, "mpv_network_timeout_seconds", 15) or 15),
        )
        render_profile = _normalize_render_profile(
            getattr(self._config, "mpv_render_profile", "auto")
        )
        options.update(_render_profile_options(render_profile))
        mpv_config_dir = resolve_mpv_config_dir()
        if mpv_config_dir is not None:
            options["config"] = True
            options["config_dir"] = str(mpv_config_dir)
            logger.info(
                "Loading user mpv config dir=%s",
                mpv_config_dir,
                extra={"log_category": "player", "log_source": "app"},
            )
        mismatch = detect_linux_nvidia_driver_mismatch()
        if mismatch is not None:
            userspace_version, kernel_version = mismatch
            options["hwdec"] = "no"
            options["vo"] = "wlshm" if os.getenv("WAYLAND_DISPLAY") and not os.getenv("DISPLAY") else "x11"
            logger.warning(
                "Detected NVIDIA driver mismatch userspace=%s kernel=%s forcing software video output",
                userspace_version,
                kernel_version,
            )
        return options

    def _collect_windows_mpv_runtime_diagnostics(self, mpv_module: Any | None = None) -> dict[str, object]:
        diagnostics: dict[str, object] = {
            "python_version": sys.version.splitlines()[0],
            "python_executable": sys.executable,
            "platform": platform.platform(),
            "find_library_mpv": ctypes.util.find_library("mpv") or "",
            "mpv_dylib_path": str(os.getenv("MPV_DYLIB_PATH") or ""),
            "path_entry_count": len([entry for entry in str(os.getenv("PATH") or "").split(os.pathsep) if entry]),
            "candidate_mpv_dlls": _existing_windows_mpv_dll_candidates(),
            "loaded_mpv_dlls": _loaded_windows_mpv_dll_paths(),
        }
        diagnostics.update(custom_mpv_library_diagnostics())
        if mpv_module is not None:
            diagnostics["mpv_module_file"] = str(getattr(mpv_module, "__file__", "") or "")
            diagnostics["mpv_module_name"] = str(getattr(mpv_module, "__name__", "") or "")
            backend = getattr(mpv_module, "_mpv", None)
            if backend is not None:
                diagnostics["mpv_backend_name"] = str(getattr(backend, "_name", "") or "")
                handle = getattr(backend, "_handle", 0) or 0
                diagnostics["mpv_backend_handle"] = hex(handle) if isinstance(handle, int) else str(handle)
        return diagnostics

    def _log_windows_mpv_runtime_diagnostics(
        self,
        stage: str,
        *,
        mpv_module: Any | None = None,
        exc: BaseException | None = None,
        force: bool = False,
    ) -> None:
        if not sys.platform.startswith("win"):
            return
        if not force and stage in _WINDOWS_MPV_DIAGNOSTIC_STAGES_LOGGED:
            return
        _WINDOWS_MPV_DIAGNOSTIC_STAGES_LOGGED.add(stage)
        diagnostics = self._collect_windows_mpv_runtime_diagnostics(mpv_module)
        if exc is None:
            logger.info(
                "MPV runtime diagnostics stage=%s data=%s",
                stage,
                diagnostics,
                extra={"log_category": "player", "log_source": "app"},
            )
            return
        diagnostics["error"] = repr(exc)
        logger.warning(
            "MPV runtime diagnostics stage=%s data=%s",
            stage,
            diagnostics,
            extra={"log_category": "player", "log_source": "app"},
        )

    def _create_player(self):
        prepare_custom_mpv_library()
        if sys.platform.startswith("win"):
            self._log_windows_mpv_runtime_diagnostics("before-import")
        try:
            import mpv
        except Exception as exc:
            if sys.platform.startswith("win"):
                self._log_windows_mpv_runtime_diagnostics("import-failed", exc=exc, force=True)
            raise
        if sys.platform.startswith("win"):
            self._log_windows_mpv_runtime_diagnostics("after-import", mpv_module=mpv)

        common = self._base_player_options()
        ytdlp_path = resolve_mpv_ytdlp_path()
        if ytdlp_path:
            common["script_opts"] = f"ytdl_hook-ytdl_path={ytdlp_path}"
        ytdl_raw_options = resolve_mpv_ytdl_raw_options(
            cookie_browser=str(getattr(self._config, "youtube_cookie_browser", "") or "")
        )
        if ytdl_raw_options:
            common["ytdl_raw_options"] = ytdl_raw_options
        if os.getenv("ATV_MPV_DEBUG"):
            common["log_handler"] = print
            common["loglevel"] = "debug"

        def instantiate(options: dict[str, object]):
            if sys.platform.startswith("win"):
                return mpv.MPV(
                    **options,
                    audio_device="auto",
                    audio_exclusive="no",
                )
            if sys.platform == "darwin":
                return mpv.MPV(
                    **options,
                    # macOS 👉 不指定最稳
                    # audio_device="auto" 也可以
                    audio_exclusive="no",
                )
            return mpv.MPV(
                **options,
                ao="pulse,pipewire,alsa,",
            )

        if sys.platform.startswith("win"):
            self._log_windows_mpv_runtime_diagnostics("before-create", mpv_module=mpv)
        original_exc: Exception | None = None
        try:
            player = instantiate(common)
        except Exception as exc:
            original_exc = exc
            player = None
            for fallback_name, render_options in _fallback_render_options(common):
                fallback_common = dict(common)
                for key in (
                    "vo",
                    "gpu_api",
                    "hwdec",
                    "profile",
                    "scale",
                    "cscale",
                    "sigmoid_upscaling",
                    "deband",
                    "interpolation",
                    "vd_lavc_threads",
                ):
                    fallback_common.pop(key, None)
                fallback_common.update(render_options)
                logger.warning(
                    "MPV create failed, retrying with render profile %s: %r",
                    fallback_name,
                    exc,
                    extra={"log_category": "player", "log_source": "app"},
                )
                try:
                    player = instantiate(fallback_common)
                    break
                except Exception as fallback_exc:
                    exc = fallback_exc
            if player is None:
                assert original_exc is not None
                exc = original_exc
                if sys.platform.startswith("win"):
                    self._log_windows_mpv_runtime_diagnostics(
                        "create-player-failed",
                        mpv_module=mpv,
                        exc=exc,
                        force=True,
                    )
                raise exc
        except BaseException:
            raise
        if original_exc is not None and player is None:
            if sys.platform.startswith("win"):
                self._log_windows_mpv_runtime_diagnostics(
                    "create-player-failed",
                    mpv_module=mpv,
                    exc=original_exc,
                    force=True,
                )
            raise original_exc
        if sys.platform.startswith("win"):
            self._log_windows_mpv_runtime_diagnostics("after-create", mpv_module=mpv)
        # mpv 0.27~0.38 只接受 yes/no,构造参数里带 auto 会让整个实例化失败,
        # 因此延后到实例存活后再按能力设置。
        self._apply_deinterlace_preference(player)
        return player

    def _apply_deinterlace_preference(self, player: Any | None = None) -> None:
        target = self._player if player is None else player
        if target is None or getattr(target, "core_shutdown", False):
            return
        for value in ("auto", "no"):
            try:
                if hasattr(type(target), "__setitem__"):
                    target["deinterlace"] = value
                else:
                    target.deinterlace = value
            except Exception:
                continue
            if target is self._player:
                self._player_property_cache["deinterlace"] = value
            if value != "auto":
                logger.info(
                    "当前 libmpv 不支持 deinterlace=auto,已回退为 no",
                    extra={"log_category": "player", "log_source": "app"},
                )
            return

    def _ensure_player(self) -> None:
        if self._player is not None and not getattr(self._player, "core_shutdown", False):
            return
        self._player = self._create_player()
        self._player_property_cache.clear()
        self._register_player_events()

    def _warm_up_player(self) -> None:
        if self._player is not None and not getattr(self._player, "core_shutdown", False):
            return
        started_at = time.monotonic()
        logger.info("MPV warm-up start")
        self._ensure_player()
        logger.info("MPV warm-up done elapsed=%.3fs", time.monotonic() - started_at)

    def warm_up_async(self) -> None:
        if self._player is not None and not getattr(self._player, "core_shutdown", False):
            return
        QTimer.singleShot(0, self._warm_up_player)

    def shutdown(self) -> None:
        if not self._on_widget_thread():
            self._run_on_widget_thread(self.shutdown)
            return
        self._windows_file_loaded_timer.stop()
        self._audio_starvation_timer.stop()
        if self._player is None:
            return
        player, self._player = self._player, None
        self._player_property_cache.clear()
        if getattr(player, "core_shutdown", False):
            return
        self._terminate_player_off_thread(player)

    def _terminate_player_off_thread(self, player: Any) -> None:
        finished = threading.Event()

        def _terminate() -> None:
            try:
                terminate = getattr(player, "terminate", None)
                if terminate is not None:
                    terminate()
            except Exception:
                if not getattr(player, "core_shutdown", False):
                    logger.warning("MPV terminate raised", exc_info=True)
            finally:
                finished.set()

        thread = threading.Thread(target=_terminate, name="mpv-terminate", daemon=True)
        thread.start()

        def _report_stuck_terminate() -> None:
            if not finished.is_set():
                logger.error(
                    "MPV terminate still blocked after %.0fs (wedged demuxer?);"
                    " leaking the player instance to keep the UI responsive",
                    _MPV_TERMINATE_TIMEOUT_SECONDS,
                )

        timer = threading.Timer(_MPV_TERMINATE_TIMEOUT_SECONDS, _report_stuck_terminate)
        timer.daemon = True
        timer.start()

    def stop_media(self) -> None:
        if not self._on_widget_thread():
            self._run_on_widget_thread(self.stop_media)
            return
        self._windows_file_loaded_timer.stop()
        player = self._player
        if player is None or getattr(player, "core_shutdown", False):
            return
        try:
            command = getattr(player, "command", None)
            if callable(command):
                command("stop")
                self._set_video_picture_state("idle")
        except Exception:
            if getattr(player, "core_shutdown", False):
                return
            raise

    def suspend(self) -> None:
        self.shutdown()

    def _register_player_events(self) -> None:
        if self._player is None:
            return
        event_callback = getattr(self._player, "event_callback", None)
        if event_callback is None:
            return

        @event_callback("end-file")
        def handle_end_file(event) -> None:
            event_data = getattr(event, "data", None)
            if event_data is None:
                return
            reason = getattr(event_data, "reason", None)
            eof_reason = getattr(type(event_data), "EOF", 0)
            error_reason = getattr(type(event_data), "ERROR", 4)
            error_text = self._format_mpv_error(getattr(event_data, "error", ""))

            def emit_event() -> None:
                self._audio_starvation_timer.stop()
                self._audio_starvation_samples.clear()
                if reason == eof_reason:
                    self._emit_playback_finished_once()
                    return
                if reason == error_reason:
                    if error_text:
                        self.playback_failed.emit(f"播放失败: {error_text}")
                    else:
                        self.playback_failed.emit("播放失败: 未知错误")

            self._post_to_widget_thread(emit_event)

        self._end_file_handler = handle_end_file

        @event_callback("file-loaded")
        def handle_file_loaded(*_args) -> None:
            self._telemetry.clear()
            self._post_to_widget_thread(self.file_loaded.emit)
            self._post_to_widget_thread(self._retry_pending_external_audio)

        self._file_loaded_handler = handle_file_loaded
        observe_property = getattr(self._player, "observe_property", None)
        if observe_property is None:
            return

        def handle_track_list(_property_name, _tracks) -> None:
            self.subtitle_tracks_changed.emit()
            self.audio_tracks_changed.emit()
            normalized = _tracks or []
            has_video_track = any(isinstance(track, dict) and track.get("type") == "video" for track in normalized)
            if has_video_track:
                self._audio_cover_active = False
                return
            if self._audio_cover_active:
                self._set_video_picture_state("audio-cover")
                return
            self._set_video_picture_state("unavailable")

        observe_property("track-list", handle_track_list)
        self._track_list_handler = handle_track_list

        def handle_chapter_list(_property_name, _chapters) -> None:
            self.chapters_changed.emit()

        observe_property("chapter-list", handle_chapter_list)
        self._chapter_list_handler = handle_chapter_list

        def handle_video_out_params(_property_name, params) -> None:
            self._video_out_params = dict(params) if isinstance(params, dict) else {}
            if params:
                if self._audio_cover_active:
                    self._set_video_picture_state("audio-cover")
                    return
                self._set_video_picture_state("visible")

        observe_property("video-out-params", handle_video_out_params)
        self._video_out_params_handler = handle_video_out_params

        def handle_eof_reached(_property_name, reached) -> None:
            if reached and self._audio_cover_mode:
                self._emit_playback_finished_once()

        observe_property("eof-reached", handle_eof_reached)
        self._eof_reached_handler = handle_eof_reached

        def handle_pause_changed(_property_name, paused) -> None:
            if paused is None:
                return
            self.pause_state_changed.emit(bool(paused))

        observe_property("pause", handle_pause_changed)
        self._pause_changed_handler = handle_pause_changed

        self._telemetry_handlers = []
        for telemetry_property in ("video-params", "video-format", "hwdec-current", "container-fps"):
            def handle_telemetry_property(_property_name, value, _key=telemetry_property) -> None:
                # 只写缓存不碰 GUI;徽章由播放器窗口的 1Hz 定时器组装刷新。
                self._telemetry[_key] = value

            observe_property(telemetry_property, handle_telemetry_property)
            self._telemetry_handlers.append(handle_telemetry_property)

        register_key_binding = getattr(self._player, "register_key_binding", None)
        if register_key_binding is None:
            return

        def handle_right_click(*_args) -> None:
            self.context_menu_requested.emit()

        def handle_left_click(*_args) -> None:
            # wid 嵌入时 mpv 子窗口直接拦截鼠标,Qt 收不到事件,左键语义经信号转发。
            self.left_clicked.emit()
            self.context_menu_dismiss_requested.emit()

        register_key_binding("MBTN_RIGHT", handle_right_click, mode="force")
        register_key_binding("MBTN_LEFT", handle_left_click, mode="force")
        self._right_click_handler = handle_right_click
        self._left_click_handler = handle_left_click

    def _build_http_header_fields(self, headers: dict[str, str] | None) -> list[str]:
        if not headers:
            return []
        return [f"{key}: {value}" for key, value in headers.items()]

    def _apply_http_header_fields(self, player: Any, header_fields: list[str]) -> None:
        if not hasattr(type(player), "__setitem__"):
            return
        player["http-header-fields"] = header_fields

    def _is_local_iso_proxy_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.path.startswith("/iso/")

    def _is_local_dash_proxy_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.path.startswith("/dash/")

    def _apply_stream_profile(
        self,
        player: Any,
        url: str,
        *,
        audio_files: str = "",
        ytdl_format: str = "",
    ) -> str:
        default_profile = dict(_DEFAULT_STREAM_PROFILE)
        default_profile["demuxer-readahead-secs"] = int(
            getattr(self._config, "mpv_default_readahead_secs", 20) or 20
        )
        if self._is_local_iso_proxy_url(url):
            profile = _ISO_PROXY_STREAM_PROFILE
            profile_name = "iso-proxy"
        elif ytdl_format:
            profile = _YTDL_STREAM_PROFILE
            profile_name = "hybrid-ytdl"
        elif audio_files and self._is_local_dash_proxy_url(url):
            profile = _DASH_DIRECT_STREAM_PROFILE
            profile_name = "dash-direct-external-audio"
        elif audio_files:
            # Separate remote video/audio streams pay the startup cost twice if we keep
            # mpv's initial cache pause enabled.
            profile = _LOW_LATENCY_STREAM_PROFILE
            profile_name = "low-latency-external-audio"
        elif self._is_local_dash_proxy_url(url):
            # DASH proxy needs some buffering for remote media, but a full initial cache pause
            # makes first-frame startup feel much slower than direct playback.
            profile = _YTDL_STREAM_PROFILE
            profile_name = "dash-proxy"
        else:
            profile = default_profile
            profile_name = "default"
        for key, value in profile.items():
            self._set_player_property(key, value)
        return profile_name

    def _apply_extra_mpv_options(self, player: Any) -> None:
        raw = str(getattr(self._config, "mpv_extra_options", "") or "").strip()
        if not raw:
            return
        previous_player = self._player
        self._player = player
        try:
            for line in raw.splitlines():
                normalized = line.strip()
                if not normalized:
                    continue
                key, value = normalized.split("=", 1)
                self._set_player_property(key.strip(), value.strip())
        finally:
            self._player = previous_player

    def shader_presets(self) -> dict[str, ShaderPreset]:
        return {preset.name: preset for preset in discover_shader_presets()}

    def apply_shader_preset(self, preset_name: str) -> bool:
        """运行时切换着色器预设;空名清除。预设不存在或设置失败返回 False。"""
        shader_files: list[str] = []
        if preset_name:
            preset = self.shader_presets().get(preset_name)
            if preset is None:
                return False
            shader_files = list(preset.shader_files)
        return self._set_glsl_shaders(shader_files)

    def _apply_shader_preset(self, player: Any) -> None:
        preset_name = str(getattr(self._config, "mpv_shader_preset", "") or "").strip()
        if not preset_name:
            return
        preset = self.shader_presets().get(preset_name)
        if preset is None:
            return
        previous_player = self._player
        self._player = player
        try:
            self._set_glsl_shaders(list(preset.shader_files))
        finally:
            self._player = previous_player

    def _set_glsl_shaders(self, shader_files: list[str]) -> bool:
        player = self._player
        if player is None or getattr(player, "core_shutdown", False):
            return False
        try:
            self._set_player_property("glsl-shaders", shader_files)
            return True
        except Exception as first_exc:
            # 个别旧版 libmpv 不接受 node 数组,退回逗号分隔字符串
            try:
                self._set_player_property("glsl-shaders", ",".join(shader_files))
                return True
            except Exception:
                logger.warning(
                    "Failed to set glsl-shaders files=%s error=%r",
                    shader_files,
                    first_exc,
                    extra={"log_category": "player", "log_source": "app"},
                )
                return False

    def _loadfile_options(self, url: str) -> dict[str, str]:
        lowered_path = urlparse(url).path.lower()
        lowered = url.lower()
        if self._is_local_iso_proxy_url(url):
            return {
                "demuxer_lavf_format": "mpegts",
                "demuxer_lavf_linearize_timestamps": "yes",
                "rebase_start_time": "yes",
            }
        if lowered_path.endswith(".mkv"):
            return {"demuxer_mkv_subtitle_preroll_secs": "0"}
        if ".m3u8" not in lowered and ".mpd" not in lowered:
            return {}
        # Some HLS/DASH sources use fragment URLs mpv would otherwise reject by extension.
        return {"demuxer_lavf_o_add": "allowed_extensions=ALL"}

    def _encode_loadfile_options(self, options: dict[str, str]) -> str:
        # mpv's option list uses commas between entries, so option values must stay comma-free.
        # Values may still contain "=" because some mpv suboptions are expressed as nested key=value text.
        for key, value in options.items():
            if "," in key or "," in value:
                raise ValueError(f"mpv loadfile option {key!r} cannot contain ','")
        return ",".join(f"{key}={value}" for key, value in options.items())

    def _loadfile_index_supported(self, player: Any) -> bool:
        mpv_version = getattr(player, "mpv_version_tuple", None)
        return isinstance(mpv_version, tuple) and mpv_version >= (0, 38, 0)

    def _should_use_async_loadfile(self, player: Any, mode: str) -> bool:
        if sys.platform.startswith("win"):
            return False
        command_async = getattr(player, "command_async", None)
        if not callable(command_async):
            return False
        if mode != "replace":
            return True
        mpv_version = getattr(player, "mpv_version_tuple", None)
        if isinstance(mpv_version, tuple) and mpv_version < (0, 38, 0):
            return not bool(self._player_property("path", ""))
        return True

    def _load_player_media(
        self,
        player: Any,
        url: str,
        *,
        mode: str = "replace",
        index: int | None = None,
        options: dict[str, str] | None = None,
    ) -> None:
        normalized_options = dict(options or {})
        if index is not None:
            player.loadfile(url, mode, index, **normalized_options)
            return
        player.loadfile(url, mode, **normalized_options)

    def _player_property(self, name: str, default: object | None = None) -> object | None:
        if self._player is None:
            return default
        try:
            return self._player[name]
        except Exception:
            if hasattr(self._player, name.replace("-", "_")):
                return getattr(self._player, name.replace("-", "_"))
            return default

    def _set_player_property(self, name: str, value: object) -> None:
        if self._player is None:
            return
        try:
            if hasattr(type(self._player), "__setitem__"):
                self._player[name] = value
            else:
                setattr(self._player, name.replace("-", "_"), value)
            self._player_property_cache[name] = value
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def apply_runtime_video_output_settings(self) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(self.apply_runtime_video_output_settings)
            return
        if self._player is None:
            return
        render_profile = _normalize_render_profile(
            getattr(self._config, "mpv_render_profile", "auto")
        )
        hwdec = str(_render_profile_options(render_profile).get("hwdec", "auto-safe"))
        if sys.platform.startswith("win") and hwdec == "auto-copy":
            hwdec = "auto-safe"
        self._set_player_property("hwdec", hwdec)
        self._apply_deinterlace_preference()

    def _is_missing_mpv_property_error(self, exc: Exception) -> bool:
        return "property does not exist" in str(exc)

    def _format_mpv_error(self, error: object | None) -> str:
        if isinstance(error, bool):
            return str(error)
        if isinstance(error, int):
            message = _MPV_ERROR_MESSAGES.get(error)
            return f"{message} ({error})" if message else str(error)
        normalized = str(error or "").strip()
        if not normalized:
            return ""
        try:
            error_code = int(normalized)
        except ValueError:
            return normalized
        message = _MPV_ERROR_MESSAGES.get(error_code)
        return f"{message} ({error_code})" if message else normalized

    def _format_end_file_failure_message(self, event_data: object | None) -> str:
        error = self._format_mpv_error(getattr(event_data, "error", ""))
        if error:
            return f"播放失败: {error}"
        return "播放失败: 未知错误"

    def _int_property_value(self, value: object | None, default: int) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return default
        return default

    def _seconds_property_value(self, value: object | None) -> int:
        if isinstance(value, bool) or value is None:
            return 0
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError, OverflowError):
            return 0

    def _scale_property_percent(self, value: object | None, default: int) -> int:
        if isinstance(value, bool):
            return int(round(float(value) * 100))
        if isinstance(value, (int, float)):
            return int(round(float(value) * 100))
        if isinstance(value, str):
            try:
                return int(round(float(value) * 100))
            except ValueError:
                return default
        return default

    def _ass_override_value(self, value: object | None, default: str) -> str:
        allowed = {"yes", "no", "force", "strip", "scale"}
        if isinstance(value, bool):
            return "yes" if value else "no"
        normalized = str(value or "").strip().lower()
        if normalized in allowed:
            return normalized
        return default

    def _yes_no_value(self, value: object | None, default: str) -> str:
        if isinstance(value, bool):
            return "yes" if value else "no"
        normalized = str(value or "").strip().lower()
        if normalized in {"yes", "no"}:
            return normalized
        return default

    def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
            audio_files: str = "",
            ytdl_format: str = "",
    ) -> None:
        if not self._on_widget_thread():
            self._run_on_widget_thread(
                lambda: self.load(
                    url,
                    pause=pause,
                    start_seconds=start_seconds,
                    headers=headers,
                    poster_image_path=poster_image_path,
                    audio_files=audio_files,
                    ytdl_format=ytdl_format,
                )
            )
            return
        load_started_at = time.monotonic()
        self._set_video_picture_state("loading")
        self._video_out_params = {}
        self._audio_cover_active = False
        self._audio_cover_mode = bool(poster_image_path)
        self._playback_finished_emitted = False
        self._windows_file_loaded_timer.stop()
        self._player_property_cache.clear()
        self._pending_external_audio_files = ""
        self._reset_audio_starvation_state(audio_files or "")
        ensure_started_at = time.monotonic()
        self._ensure_player()
        ensure_elapsed = time.monotonic() - ensure_started_at
        player = self._player
        if player is None:
            return
        setup_started_at = time.monotonic()
        header_fields = self._build_http_header_fields(headers)
        loadfile_options = self._loadfile_options(url)
        if ytdl_format:
            loadfile_options["ytdl"] = "yes"
            loadfile_options["ytdl_format"] = ytdl_format
        can_loadfile = hasattr(player, "loadfile")
        try:
            self._apply_http_header_fields(player, header_fields)
            profile_name = self._apply_stream_profile(
                player,
                url,
                audio_files=audio_files,
                ytdl_format=ytdl_format,
            )
            self._apply_extra_mpv_options(player)
            self._apply_shader_preset(player)
            logger.info(
                "MPV load url=%s audio=%s ytdl_format=%s start=%s pause=%s profile=%s headers=%s elapsed_before_command=%.3fs ensure=%.3fs setup=%.3fs",
                self._summarize_media_url(url),
                self._summarize_media_url(audio_files),
                ytdl_format,
                start_seconds,
                pause,
                profile_name,
                bool(header_fields),
                time.monotonic() - load_started_at,
                ensure_elapsed,
                time.monotonic() - setup_started_at,
            )
            if poster_image_path and can_loadfile:
                self._load_media(
                    player,
                    url,
                    start_seconds,
                    {
                        **loadfile_options,
                        "cover_art_files": poster_image_path,
                        "audio_display": "external-first",
                    },
                )
            elif poster_image_path:
                self._load_media(player, url, start_seconds, loadfile_options)
                self.attach_audio_cover(poster_image_path)
            elif start_seconds > 0 and can_loadfile:
                self._load_player_media(
                    player,
                    url,
                    options={**loadfile_options, "start": str(start_seconds)},
                )
            elif audio_files and can_loadfile:
                self._load_player_media(player, url, options=loadfile_options)
            elif (header_fields or loadfile_options) and can_loadfile:
                self._load_player_media(player, url, options=loadfile_options)
            else:
                player.play(url)
            self._attach_external_audio(player, audio_files)
        except Exception:
            if getattr(player, "core_shutdown", False):
                player = self._create_player()
                self._player = player
                self._register_player_events()
                self._apply_http_header_fields(player, header_fields)
                profile_name = self._apply_stream_profile(
                    player,
                    url,
                    audio_files=audio_files,
                    ytdl_format=ytdl_format,
                )
                self._apply_extra_mpv_options(player)
                self._apply_shader_preset(player)
                logger.info(
                    "MPV reload after player restart url=%s audio=%s ytdl_format=%s start=%s pause=%s profile=%s headers=%s",
                    self._summarize_media_url(url),
                    self._summarize_media_url(audio_files),
                    ytdl_format,
                    start_seconds,
                    pause,
                    profile_name,
                    bool(header_fields),
                )
                can_loadfile = hasattr(player, "loadfile")
                if poster_image_path and can_loadfile:
                    self._load_media(
                        player,
                        url,
                        start_seconds,
                        {
                            **loadfile_options,
                            "cover_art_files": poster_image_path,
                            "audio_display": "external-first",
                        },
                    )
                elif poster_image_path:
                    self._load_media(player, url, start_seconds, loadfile_options)
                    self.attach_audio_cover(poster_image_path)
                elif start_seconds > 0 and can_loadfile:
                    self._load_player_media(
                        player,
                        url,
                        options={**loadfile_options, "start": str(start_seconds)},
                    )
                elif audio_files and can_loadfile:
                    self._load_player_media(player, url, options=loadfile_options)
                elif (header_fields or loadfile_options) and can_loadfile:
                    self._load_player_media(player, url, options=loadfile_options)
                else:
                    player.play(url)
                self._attach_external_audio(player, audio_files)
            else:
                raise
        player.pause = pause

    def _summarize_media_url(self, url: str) -> str:
        parsed = urlparse(url or "")
        if not parsed.scheme or not parsed.netloc:
            return url
        path = parsed.path or "/"
        if len(path) > 96:
            path = f"...{path[-96:]}"
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    def _load_media(self, player: Any, url: str, start_seconds: int, loadfile_options: dict[str, str]) -> None:
        can_loadfile = hasattr(player, "loadfile")
        if start_seconds > 0 and can_loadfile:
            self._load_player_media(
                player,
                url,
                options={**loadfile_options, "start": str(start_seconds)},
            )
            return
        if loadfile_options and can_loadfile:
            self._load_player_media(player, url, options=loadfile_options)
            return
        player.play(url)

    def _attach_external_audio(self, player: Any, audio_files: str) -> None:
        if not audio_files:
            self._external_audio_files = ""
            return
        self._external_audio_files = audio_files
        self._audio_starvation_samples.clear()
        self._audio_starvation_reloads = 0
        try:
            self._audio_add_command(player, audio_files)
        except Exception:
            if getattr(player, "core_shutdown", False):
                return
            self._pending_external_audio_files = audio_files
            logger.warning(
                "audio-add right after loadfile failed (AO init race), "
                "will verify after file-loaded: %s",
                self._summarize_media_url(audio_files),
                exc_info=True,
            )

    def _retry_pending_external_audio(self) -> None:
        audio_files = self._pending_external_audio_files
        if not audio_files:
            return
        self._pending_external_audio_files = ""
        player = self._player
        if player is None or getattr(player, "core_shutdown", False):
            return
        tracks = self._player_property("track-list") or []
        has_external_audio = any(
            isinstance(track, dict)
            and track.get("type") == "audio"
            and track.get("external")
            for track in tracks
        )
        if has_external_audio:
            return
        try:
            self._audio_add_command(player, audio_files)
        except Exception:
            if not getattr(player, "core_shutdown", False):
                logger.warning(
                    "external audio retry after file-loaded failed: %s",
                    self._summarize_media_url(audio_files),
                    exc_info=True,
                )
                self.external_audio_attach_failed.emit(audio_files)

    def _audio_add_command(self, player: Any, audio_files: str) -> None:
        audio_add = getattr(player, "audio_add", None)
        if callable(audio_add):
            audio_add(audio_files)
            return
        command = getattr(player, "command", None)
        if callable(command):
            command("audio-add", audio_files, "select")
            return
        if hasattr(player, "loadfile"):
            player.loadfile(audio_files, "append")

    def _reset_audio_starvation_state(self, audio_files: str) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self._reset_audio_starvation_state(audio_files))
            return
        self._external_audio_files = audio_files
        self._audio_starvation_samples.clear()
        self._audio_starvation_last_fire = 0.0
        self._audio_starvation_reloads = 0
        if audio_files:
            self._audio_starvation_timer.start()
        else:
            self._audio_starvation_timer.stop()

    def _probe_audio_pts_property(self, player: Any) -> bool:
        try:
            getattr(player, "audio_pts")
        except Exception as exc:
            if self._is_missing_mpv_property_error(exc):
                logger.info("当前 libmpv 不支持 audio-pts 属性,禁用外挂音轨断粮看门狗")
                return False
            # 暂态读取失败:先当作支持,后续采样再实际读
        return True

    def _check_external_audio_starvation(self) -> None:
        audio_files = self._external_audio_files
        if not audio_files or self._pending_external_audio_files:
            self._audio_starvation_samples.clear()
            return
        player = self._player
        if player is None or getattr(player, "core_shutdown", False):
            return
        if self._audio_pts_supported is None:
            self._audio_pts_supported = self._probe_audio_pts_property(player)
        if not self._audio_pts_supported:
            self._audio_starvation_timer.stop()
            return
        # 运行时属性必须走 getattr(_player_property 的回退路径):
        # python-mpv 的 player["x"] 走 options/ 前缀,对 playback-time 等
        # 运行时属性会直接抛 "property does not exist"。
        paused = self._player_property("pause")
        if paused is None:
            return
        playback_time = self._player_property("playback-time")
        audio_pts = self._player_property("audio-pts")
        tracks = self._player_property("track-list") or []
        if paused:
            self._audio_starvation_samples.clear()
            return
        if not any(
            isinstance(track, dict)
            and track.get("type") == "audio"
            and track.get("external")
            and track.get("selected")
            for track in tracks
        ):
            self._audio_starvation_samples.clear()
            return
        now = time.monotonic()
        samples = self._audio_starvation_samples
        samples.append((now, (playback_time, audio_pts)))
        while samples and now - samples[0][0] > _AUDIO_STARVATION_WINDOW_SECONDS:
            samples.pop(0)
        if len(samples) < _AUDIO_STARVATION_MIN_SAMPLES:
            return
        # 音频是 A/V 同步主:上游硬断时 playback-time 会连同 audio-pts 一起
        # 冻结(端到端实测),劣化渗透时 audio-pts 冻结而画面继续。两种形态
        # 都按"外挂音轨断粮"处理,重挂上限兜底误报。
        if len({repr(sample[1][1]) for sample in samples}) > 1:
            return  # audio-pts 在动 = 仍在出声
        self._fire_external_audio_starvation(audio_files)

    def _fire_external_audio_starvation(self, audio_files: str) -> None:
        now = time.monotonic()
        if now - self._audio_starvation_last_fire < _AUDIO_STARVATION_COOLDOWN_SECONDS:
            return
        if self._audio_starvation_reloads >= _AUDIO_STARVATION_MAX_RELOADS:
            self._audio_starvation_timer.stop()
            logger.warning(
                "外挂音轨断粮重挂已达上限(%d 次),停止看门狗: %s",
                self._audio_starvation_reloads,
                self._summarize_media_url(audio_files),
            )
            self.external_audio_attach_failed.emit(audio_files)
            return
        self._audio_starvation_last_fire = now
        self._audio_starvation_reloads += 1
        self._audio_starvation_samples.clear()
        logger.warning(
            "外挂音轨断粮(audio-pts 冻结而播放在走),第 %d 次触发重挂: %s",
            self._audio_starvation_reloads,
            self._summarize_media_url(audio_files),
        )
        self.external_audio_starved.emit(audio_files)

    def reload_external_audio(self) -> bool:
        """audio-reload:重新打开当前外挂音轨(重读其 URL,代理可借机换上游)。"""
        if not self._on_widget_thread():
            return bool(self._run_on_widget_thread(self.reload_external_audio))
        player = self._player
        if player is None or getattr(player, "core_shutdown", False):
            return False
        try:
            command = getattr(player, "command", None)
            if callable(command):
                # 不带 track id = 重载当前音轨;python-mpv 的 audio_reload()
                # 无参调用会把 None 传给 mpv 报 -4,不能用
                command("audio-reload")
                return True
            audio_reload = getattr(player, "audio_reload", None)
            if callable(audio_reload):
                audio_reload()
                return True
        except Exception:
            if not getattr(player, "core_shutdown", False):
                logger.warning("audio-reload 失败", exc_info=True)
        return False

    # ── 直播弹幕 OSD(ass-events 由 live_danmaku 渲染器产出) ───────────

    def present_live_danmaku(self, data: str | None, res_w: int, res_h: int) -> None:
        """把 ASS 事件帧交给 mpv osd-overlay 合成;data 为 None 表示清除。

        弹幕画在 mpv 自己的 OSD 里(Wayland/XWayland 下 Qt 原生叠加层
        无法透明),坐标空间 res_w/res_h 即视频窗口像素。渲染循环跑在
        独立线程(python-mpv 命令线程安全),不经 UI 线程转发,避免
        30fps 同步命令被 UI 阻塞造成滚动卡顿。
        """
        player = self._player
        if player is None or getattr(player, "core_shutdown", False):
            return
        try:
            if data:
                player.command(
                    "osd-overlay",
                    id=_LIVE_DANMAKU_OSD_ID,
                    format="ass-events",
                    data=data,
                    res_x=max(1, res_w),
                    res_y=max(1, res_h),
                )
            else:
                # 本机 mpv 不接受 format="none"(python-mpv 的 osd_overlay_remove
                # 同样报 -4);空 ass-events 数据等效清屏。
                player.command(
                    "osd-overlay",
                    id=_LIVE_DANMAKU_OSD_ID,
                    format="ass-events",
                    data="",
                    res_x=16,
                    res_y=16,
                )
        except Exception:
            if not getattr(player, "core_shutdown", False):
                logger.debug("live danmaku osd update failed", exc_info=True)

    def clear_live_danmaku(self) -> None:
        self.present_live_danmaku(None, 0, 0)

    def attach_audio_cover(self, poster_image_path: str) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.attach_audio_cover(poster_image_path))
            return
        player = self._player
        if player is None or not poster_image_path:
            return
        try:
            self._set_player_property("audio-display", "external-first")
            self._set_player_property("image-display-duration", "inf")
            self._set_player_property("keep-open", "yes")
            player.command("video-add", poster_image_path, "select", "", "", True)
        except Exception:
            self._audio_cover_active = False
            self._audio_cover_mode = False
            if getattr(player, "core_shutdown", False):
                return
            self._set_video_picture_state("unavailable")
            return
        self._audio_cover_active = True
        self._audio_cover_mode = True
        self._set_video_picture_state("audio-cover")

    def seek(self, seconds: int) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.seek(seconds))
            return
        if self._player is None:
            return
        try:
            self._player.command("seek", seconds, "absolute")
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def seek_relative(self, seconds: int) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.seek_relative(seconds))
            return
        if self._player is None:
            return
        try:
            self._player.command("seek", seconds, "relative")
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def can_seek(self) -> bool:
        if not self._on_widget_thread():
            return bool(self._run_on_widget_thread(self.can_seek))
        if self._player is None:
            return False
        try:
            return bool(self._player.seekable)
        except Exception:
            return False

    def set_speed(self, speed: float) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_speed(speed))
            return
        if self._player is None:
            return
        try:
            self._player.speed = speed
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def set_volume(self, value: int) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_volume(value))
            return
        if self._player is None:
            return
        try:
            self._player.volume = value
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def toggle_mute(self) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(self.toggle_mute)
            return
        if self._player is None:
            return
        try:
            self._player.mute = not bool(getattr(self._player, "mute", False))
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def set_muted(self, muted: bool) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_muted(muted))
            return
        if self._player is None:
            return
        try:
            self._player.mute = muted
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def set_cursor_autohide(self, value: int | None) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_cursor_autohide(value))
            return
        if self._player is None:
            return
        try:
            self._player["input-cursor"] = True
            self._player["cursor-autohide-fs-only"] = False
            self._player["cursor-autohide"] = value if value is not None else "no"
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def pause(self) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(self.pause)
            return
        if self._player is None:
            return
        try:
            self._player.pause = True
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def resume(self) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(self.resume)
            return
        if self._player is None:
            return
        try:
            self._player.pause = False
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def position_seconds(self) -> int | None:
        if not self._on_widget_thread():
            return self._run_on_widget_thread(self.position_seconds)
        if self._player is None:
            return None
        try:
            pos = self._player.time_pos
            return int(pos) if pos is not None else None
        except Exception:
            return None

    def current_video_height(self) -> int | None:
        if not self._on_widget_thread():
            return self._run_on_widget_thread(self.current_video_height)
        height = self._video_out_params.get("h")
        if isinstance(height, (int, float)) and height > 0:
            return int(height)
        return None

    def duration_seconds(self) -> int:
        if not self._on_widget_thread():
            return int(self._run_on_widget_thread(self.duration_seconds) or 0)
        if self._player is None:
            return 0
        duration = self._seconds_property_value(self._player_property("duration", None))
        if duration > 0:
            return duration
        try:
            return self._seconds_property_value(getattr(self._player, "duration", 0))
        except Exception:
            return 0

    def demuxer_cache_duration_seconds(self) -> int:
        if not self._on_widget_thread():
            return int(self._run_on_widget_thread(self.demuxer_cache_duration_seconds) or 0)
        if self._player is None:
            return 0
        return self._seconds_property_value(self._player_property("demuxer-cache-duration", None))

    def telemetry_snapshot(self) -> dict[str, object]:
        """播放遥测快照,供徽章 1Hz 刷新。

        少变项(分辨率/编码/硬解/帧率)取自事件缓存;持续项按需读取。
        码率属性以 bit/s 计,下行速率 raw-input-rate 以 byte/s 计(mpv 0.41 实测)。
        """
        player = self._player
        if player is None or getattr(player, "core_shutdown", False):
            return {}
        snapshot: dict[str, object] = {}
        video_params = self._telemetry.get("video-params")
        if isinstance(video_params, Mapping):
            snapshot["video_width"] = video_params.get("w")
            snapshot["video_height"] = video_params.get("h")
        video_format = self._telemetry.get("video-format")
        if video_format:
            snapshot["video_format"] = str(video_format)
        hwdec_current = self._telemetry.get("hwdec-current")
        if hwdec_current:
            snapshot["hwdec_current"] = str(hwdec_current)
        container_fps = self._telemetry.get("container-fps")
        if isinstance(container_fps, (int, float)):
            snapshot["container_fps"] = float(container_fps)
        snapshot["frame_drop_count"] = self._read_runtime_property("frame-drop-count")
        snapshot["cache_buffering_state"] = self._read_runtime_property("cache-buffering-state")
        snapshot["video_bitrate"] = self._read_runtime_property("video-bitrate")
        snapshot["audio_bitrate"] = self._read_runtime_property("audio-bitrate")
        snapshot["audio_codec"] = self._read_runtime_property("audio-codec")
        audio_params = self._read_runtime_property("audio-params")
        if isinstance(audio_params, Mapping):
            snapshot["audio_samplerate"] = audio_params.get("samplerate")
            snapshot["audio_channels"] = audio_params.get("channel-count")
        demuxer_state = self._read_runtime_property("demuxer-cache-state")
        if isinstance(demuxer_state, Mapping):
            snapshot["input_rate_bytes"] = demuxer_state.get("raw-input-rate")
            snapshot["cache_duration"] = demuxer_state.get("cache-duration")
        return snapshot

    def _read_runtime_property(self, name: str) -> object:
        # python-mpv 的 player[name] 走 options/ 前缀,运行时属性必须经属性访问读取。
        player = self._player
        try:
            return getattr(player, name.replace("-", "_"))
        except AttributeError:
            return None

    def _chapter_label(self, title: str, index: int) -> str:
        return title.strip() or f"章节 {index}"

    def chapters(self) -> list[Chapter]:
        if not self._on_widget_thread():
            return list(self._run_on_widget_thread(self.chapters) or [])
        if self._player is None:
            return []
        raw_chapters = self._player_property("chapter-list", None) or []
        if not isinstance(raw_chapters, (list, tuple)):
            return []

        entries: list[tuple[float, str]] = []
        for raw_chapter in raw_chapters:
            if not isinstance(raw_chapter, dict):
                continue
            raw_time = raw_chapter.get("time")
            if isinstance(raw_time, bool) or raw_time is None:
                continue
            try:
                start_seconds = float(raw_time)
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(start_seconds):
                continue
            entries.append((max(0.0, start_seconds), str(raw_chapter.get("title") or "")))

        entries.sort(key=lambda entry: entry[0])
        return [
            Chapter(
                index=index,
                title=title.strip(),
                start_seconds=start_seconds,
                label=self._chapter_label(title, index + 1),
            )
            for index, (start_seconds, title) in enumerate(entries)
        ]

    # Maps common raw subtitle track titles (language identifiers such as
    # "Simplified", "chs", "GB", "Big5") to a readable Chinese display name.
    # Titles that are not pure language identifiers (e.g. "Signs", "Director
    # Commentary") are intentionally left untouched.
    _SUBTITLE_TITLE_DISPLAY: dict[str, str] = {
        "simplified": "简体中文",
        "simplified chinese": "简体中文",
        "simplified-chinese": "简体中文",
        "简体": "简体中文",
        "简中": "简体中文",
        "简体中文": "简体中文",
        "chs": "简体中文",
        "gb": "简体中文",
        "sc": "简体中文",
        "traditional": "繁体中文",
        "traditional chinese": "繁体中文",
        "traditional-chinese": "繁体中文",
        "transitional": "繁体中文",
        "tranditional": "繁体中文",
        "繁体": "繁体中文",
        "繁體": "繁体中文",
        "繁中": "繁体中文",
        "繁体中文": "繁体中文",
        "cht": "繁体中文",
        "big5": "繁体中文",
        "tc": "繁体中文",
        "chinese": "中文",
        "中文": "中文",
        "中文字幕": "中文",
        "english": "English",
        "eng": "English",
        "japanese": "日本語",
        "jpn": "日本語",
        "日语": "日本語",
    }

    @staticmethod
    def _normalize_subtitle_title_key(title: str) -> str:
        return " ".join(title.split()).casefold().strip("()[]（）【】{}<>-_=+/\\|.,;: ")

    def _translate_subtitle_title(self, title: str) -> str:
        if not title:
            return ""
        return self._SUBTITLE_TITLE_DISPLAY.get(self._normalize_subtitle_title_key(title), "")

    def _subtitle_bilingual_label(self, title: str) -> str:
        # 识别"中英双语"类标题（如 chs&eng / cht&eng / 中英 / 简英），返回中文展示名。
        lowered = title.casefold()
        english_tokens = ("english", "eng", "英文", "英语", "英")
        if not any(token in lowered for token in english_tokens):
            return ""
        if any(token in lowered for token in ("chs", "简", "simplified", "hans", "gb", "sc")):
            return "简英双语"
        if any(token in lowered for token in ("cht", "繁", "traditional", "tranditional", "hant", "big5", "tc")):
            return "繁英双语"
        if any(token in lowered for token in ("中", "chi", "zh", "chinese")):
            return "中英双语"
        return ""

    def _subtitle_language_label(self, lang: str) -> str:
        normalized = lang.strip().lower()
        return {
            "zh": "简体中文",
            "chi": "简体中文",
            "zho": "简体中文",
            "chs": "简体中文",
            "simplified": "简体中文",
            "zh-cn": "简体中文",
            "zh-hans": "简体中文",
            "zh-tw": "繁体中文",
            "cht": "繁体中文",
            "traditional": "繁体中文",
            "zh-hant": "繁体中文",
            "en": "English",
            "eng": "English",
            "ja": "日本語",
            "jpn": "日本語",
        }.get(normalized, normalized or "")

    def _subtitle_track_label(self, title: str, lang: str, is_default: bool, is_forced: bool, index: int) -> str:
        raw_title = title.strip()
        base = (
            self._translate_subtitle_title(raw_title)
            or self._subtitle_bilingual_label(raw_title)
            or raw_title
            or self._subtitle_language_label(lang)
            or f"字幕 {index}"
        )
        suffixes = []
        if is_default:
            suffixes.append("默认")
        if is_forced:
            suffixes.append("强制")
        if not suffixes:
            return base
        return f"{base} ({'/'.join(suffixes)})"

    _SUBTITLE_CODEC_LABELS: dict[str, str] = {
        "ass": "ASS",
        "ssa": "SSA",
        "subrip": "SRT",
        "srt": "SRT",
        "webvtt": "WebVTT",
        "mov_text": "MP4",
        "hdmv_pgs_subtitle": "PGS",
        "dvd_subtitle": "VOBSUB",
        "dvb_subtitle": "DVB",
        "arib_caption": "ARIB",
    }

    def _subtitle_track_detail_parts(self, raw_track: object) -> list[str]:
        if not isinstance(raw_track, dict):
            return []
        codec = str(raw_track.get("codec") or "").strip().lower()
        if not codec:
            return []
        return [self._SUBTITLE_CODEC_LABELS.get(codec, codec.upper())]

    def _is_chinese_subtitle_track(self, track: SubtitleTrack) -> bool:
        if track.lang in {"zh", "chi", "zho", "chs", "zh-cn", "zh-hans", "zh-tw", "cht", "zh-hant"}:
            return True
        lowered_title = track.title.casefold()
        return any(
            token in lowered_title
            for token in (
                "中文", "简中", "繁中", "中字", "简体", "繁体", "繁體", "中英", "双语",
                "chinese", "simplified", "traditional", "transitional", "tranditional",
                "chs", "cht", "big5",
            )
        )

    def _chinese_subtitle_preference(self, track: SubtitleTrack) -> int:
        normalized_lang = track.lang.casefold()
        lowered_title = track.title.casefold()
        simplified_langs = {"zh", "chi", "zho", "chs", "zh-cn", "zh-hans"}
        traditional_langs = {"zh-tw", "cht", "zh-hant"}
        simplified_tokens = ("简中", "简体", "chs", "sc", "gb", "hans", "simplified")
        traditional_tokens = ("繁中", "繁體", "繁体", "cht", "tc", "big5", "hant", "traditional", "tranditional")
        english_tokens = ("english", "eng", "英文", "英语", "英")
        has_english = any(token in lowered_title for token in english_tokens)
        has_simplified = any(token in lowered_title for token in simplified_tokens)
        has_traditional = any(token in lowered_title for token in traditional_tokens)
        # 中英双语字幕优先级最高（简英 > 繁英/中英 > 简体 > 通用中文 > 繁体）
        if has_english:
            if has_simplified:
                return 4
            if has_traditional or any(token in lowered_title for token in ("中", "chi", "zh", "chinese")):
                return 3
        if has_simplified:
            return 2
        if has_traditional:
            return 0
        if normalized_lang in simplified_langs:
            return 2
        if normalized_lang in traditional_langs:
            return 0
        return 1

    def _is_english_subtitle_track(self, track: SubtitleTrack) -> bool:
        if track.lang in {"en", "eng"}:
            return True
        lowered_title = track.title.casefold()
        return "english" in lowered_title

    def _preferred_subtitle_sort_key(self, track: SubtitleTrack) -> tuple[int, int, int]:
        return (
            self._chinese_subtitle_preference(track),
            int(track.is_default),
            int(bool(track.title)),
        )

    def subtitle_tracks(self) -> list[SubtitleTrack]:
        if not self._on_widget_thread():
            return list(self._run_on_widget_thread(self.subtitle_tracks) or [])
        if self._player is None:
            return []
        try:
            raw_tracks = getattr(self._player, "track_list", None) or []
        except Exception:
            return []

        track_entries: list[tuple[int, str, str, bool, bool, object]] = []
        for raw_track in raw_tracks:
            if raw_track.get("type") != "sub" or raw_track.get("external"):
                continue
            title = str(raw_track.get("title") or "").strip()
            lang = str(raw_track.get("lang") or "").strip().lower()
            is_default = bool(raw_track.get("default"))
            is_forced = bool(raw_track.get("forced"))
            track_entries.append((int(raw_track["id"]), title, lang, is_default, is_forced, raw_track))

        base_labels = [
            self._subtitle_track_label(title, lang, is_default, is_forced, index + 1)
            for index, (_, title, lang, is_default, is_forced, _) in enumerate(track_entries)
        ]
        duplicate_labels = {label for label in base_labels if base_labels.count(label) > 1}

        tracks: list[SubtitleTrack] = []
        for index, (track_id, title, lang, is_default, is_forced, raw_track) in enumerate(track_entries):
            label = base_labels[index]
            if label in duplicate_labels:
                detail_parts = self._subtitle_track_detail_parts(raw_track)
                if detail_parts:
                    label = f"{label} [{ ' / '.join(detail_parts) }]"
            tracks.append(
                SubtitleTrack(
                    id=track_id,
                    title=title,
                    lang=lang,
                    is_default=is_default,
                    is_forced=is_forced,
                    label=label,
                )
            )
        return tracks

    def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
        if not self._on_widget_thread():
            return self._run_on_widget_thread(lambda: self.apply_subtitle_mode(mode, track_id=track_id))
        if self._player is None:
            return None
        try:
            if mode == "off":
                self._player.sid = "no"
                return None
            if mode == "track" and track_id is not None:
                self._player.sid = track_id
                return track_id
            tracks = self.subtitle_tracks()
            chinese_tracks = [track for track in tracks if self._is_chinese_subtitle_track(track)]
            english_tracks = [track for track in tracks if self._is_english_subtitle_track(track)]
            preferred_track = None
            if chinese_tracks:
                preferred_track = max(chinese_tracks, key=self._preferred_subtitle_sort_key)
            elif english_tracks:
                preferred_track = max(english_tracks, key=self._preferred_subtitle_sort_key)
            if preferred_track is not None:
                self._player.sid = preferred_track.id
                return preferred_track.id
            self._player.sid = "auto"
            return None
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return None
            raise

    def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
        if not self._on_widget_thread():
            return self._run_on_widget_thread(lambda: self.apply_secondary_subtitle_mode(mode, track_id=track_id))
        if self._player is None:
            return None
        try:
            if mode == "off":
                self._set_player_property("secondary-sid", "no")
                return None
            if mode == "track" and track_id is not None:
                self._set_player_property("secondary-sid", track_id)
                return track_id
            self._set_player_property("secondary-sid", "no")
            return None
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return None
            raise

    def current_subtitle_track_id(self) -> int | None:
        if not self._on_widget_thread():
            return self._run_on_widget_thread(self.current_subtitle_track_id)
        value = self._player_property("sid", None)
        if value in {None, "auto", "no"}:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def has_subtitle_track(self, track_id: int) -> bool:
        if not self._on_widget_thread():
            return bool(self._run_on_widget_thread(lambda: self.has_subtitle_track(track_id)))
        return track_id in self._subtitle_track_ids()

    def _subtitle_track_ids(self) -> set[int]:
        if self._player is None:
            return set()
        raw_tracks = getattr(self._player, "track_list", [])
        track_ids: set[int] = set()
        for raw_track in raw_tracks:
            if raw_track.get("type") != "sub":
                continue
            try:
                track_ids.add(int(raw_track["id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return track_ids

    def _detect_new_subtitle_track_id(self, before_ids: set[int]) -> int | None:
        for attempt in range(6):
            after_ids = self._subtitle_track_ids()
            new_ids = sorted(after_ids - before_ids)
            if new_ids:
                return new_ids[-1]
            if attempt == 5:
                break
            app = QCoreApplication.instance()
            if app is not None:
                app.processEvents()
            time.sleep(0.01)
        return None

    def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
        if not self._on_widget_thread():
            return self._run_on_widget_thread(
                lambda: self.load_external_subtitle(path, select_for_secondary=select_for_secondary)
            )
        if self._player is None:
            return None
        before_ids = self._subtitle_track_ids()
        try:
            self._player.command("sub-add", path, "auto")
            track_id = self._detect_new_subtitle_track_id(before_ids)
            if select_for_secondary and track_id is not None:
                self.apply_secondary_subtitle_mode("track", track_id=track_id)
            return track_id
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return None
            raise

    def remove_subtitle_track(self, track_id: int | None) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.remove_subtitle_track(track_id))
            return
        if self._player is None or track_id is None:
            return
        try:
            current_secondary_sid = self._player_property("secondary-sid", None)
            if str(current_secondary_sid) == str(track_id):
                self.apply_secondary_subtitle_mode("off")
            if track_id not in self._subtitle_track_ids():
                return
            self._player.command("sub-remove", track_id)
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def subtitle_position(self) -> int:
        if not self._on_widget_thread():
            return int(self._run_on_widget_thread(self.subtitle_position) or 50)
        value = self._player_property("sub-pos", 50)
        return self._int_property_value(value, 50)

    def set_subtitle_position(self, value: int) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_subtitle_position(value))
            return
        clamped = max(0, min(int(value), 100))
        self._set_player_property("sub-pos", clamped)

    def secondary_subtitle_position(self) -> int:
        if not self._on_widget_thread():
            return int(self._run_on_widget_thread(self.secondary_subtitle_position) or 50)
        value = self._player_property("secondary-sub-pos", 50)
        return self._int_property_value(value, 50)

    def supports_secondary_subtitle_position(self) -> bool:
        if not self._on_widget_thread():
            return bool(self._run_on_widget_thread(self.supports_secondary_subtitle_position))
        if self._player is None:
            return False
        try:
            self._player_property("secondary-sub-pos", 50)
            if hasattr(self._player, "__getitem__"):
                _ = self._player["secondary-sub-pos"]
            return True
        except Exception as exc:
            if self._is_missing_mpv_property_error(exc):
                return False
            if getattr(self._player, "core_shutdown", False):
                return False
            raise

    def set_secondary_subtitle_position(self, value: int) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_secondary_subtitle_position(value))
            return
        clamped = max(0, min(int(value), 100))
        self._set_player_property("secondary-sub-pos", clamped)

    def subtitle_scale(self) -> int:
        if not self._on_widget_thread():
            return int(self._run_on_widget_thread(self.subtitle_scale) or 100)
        value = self._player_property("sub-scale", 1.0)
        return self._scale_property_percent(value, 100)

    def set_subtitle_scale(self, value: int) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_subtitle_scale(value))
            return
        clamped = max(50, min(int(value), 200))
        self._set_player_property("sub-scale", clamped / 100)

    def secondary_subtitle_scale(self) -> int:
        if not self._on_widget_thread():
            return int(self._run_on_widget_thread(self.secondary_subtitle_scale) or 100)
        value = self._player_property("secondary-sub-scale", 1.0)
        return self._scale_property_percent(value, 100)

    def set_secondary_subtitle_scale(self, value: int) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_secondary_subtitle_scale(value))
            return
        clamped = max(50, min(int(value), 200))
        self._set_player_property("secondary-sub-scale", clamped / 100)

    # ── 字幕/音频延迟与画面调节(纯时间/像素属性,与字幕槽无关,可独立调节)──

    def subtitle_delay(self) -> float:
        if not self._on_widget_thread():
            return float(self._run_on_widget_thread(self.subtitle_delay) or 0.0)
        value = self._player_property("sub-delay", 0.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def set_subtitle_delay(self, seconds: float) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_subtitle_delay(seconds))
            return
        self._set_player_property("sub-delay", max(-10.0, min(float(seconds), 10.0)))

    def audio_delay(self) -> float:
        if not self._on_widget_thread():
            return float(self._run_on_widget_thread(self.audio_delay) or 0.0)
        value = self._player_property("audio-delay", 0.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def set_audio_delay(self, seconds: float) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_audio_delay(seconds))
            return
        self._set_player_property("audio-delay", max(-10.0, min(float(seconds), 10.0)))

    def supports_picture_adjustments(self) -> bool:
        """Return whether mpv is currently using copy-back hardware decoding."""
        if not self._on_widget_thread():
            return bool(self._run_on_widget_thread(self.supports_picture_adjustments))
        return str(self._player_property("hwdec", "") or "").strip().lower() == "auto-copy"

    def brightness(self) -> int:
        if not self._on_widget_thread():
            return int(self._run_on_widget_thread(self.brightness) or 0)
        return self._int_property_value(self._player_property("brightness", 0), 0)

    def set_brightness(self, value: int) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_brightness(value))
            return
        self._set_player_property("brightness", max(-100, min(int(value), 100)))

    def contrast(self) -> int:
        if not self._on_widget_thread():
            return int(self._run_on_widget_thread(self.contrast) or 0)
        return self._int_property_value(self._player_property("contrast", 0), 0)

    def set_contrast(self, value: int) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_contrast(value))
            return
        self._set_player_property("contrast", max(-100, min(int(value), 100)))

    def saturation(self) -> int:
        if not self._on_widget_thread():
            return int(self._run_on_widget_thread(self.saturation) or 0)
        return self._int_property_value(self._player_property("saturation", 0), 0)

    def set_saturation(self, value: int) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_saturation(value))
            return
        self._set_player_property("saturation", max(-100, min(int(value), 100)))

    def hue(self) -> int:
        if not self._on_widget_thread():
            return int(self._run_on_widget_thread(self.hue) or 0)
        return self._int_property_value(self._player_property("hue", 0), 0)

    def set_hue(self, value: int) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_hue(value))
            return
        self._set_player_property("hue", max(-100, min(int(value), 100)))

    def gamma(self) -> int:
        if not self._on_widget_thread():
            return int(self._run_on_widget_thread(self.gamma) or 0)
        return self._int_property_value(self._player_property("gamma", 0), 0)

    def set_gamma(self, value: int) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_gamma(value))
            return
        self._set_player_property("gamma", max(-100, min(int(value), 100)))

    def subtitle_ass_override(self) -> str:
        if not self._on_widget_thread():
            return str(self._run_on_widget_thread(self.subtitle_ass_override) or "scale")
        value = self._player_property("sub-ass-override", "scale")
        return self._ass_override_value(value, "scale")

    def set_subtitle_ass_override(self, value: str) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_subtitle_ass_override(value))
            return
        self._set_player_property("sub-ass-override", self._ass_override_value(value, "scale"))

    def secondary_subtitle_ass_override(self) -> str:
        if not self._on_widget_thread():
            return str(self._run_on_widget_thread(self.secondary_subtitle_ass_override) or "strip")
        value = self._player_property("secondary-sub-ass-override", "strip")
        return self._ass_override_value(value, "strip")

    def set_secondary_subtitle_ass_override(self, value: str) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_secondary_subtitle_ass_override(value))
            return
        self._set_player_property("secondary-sub-ass-override", self._ass_override_value(value, "strip"))

    def subtitle_ass_force_margins(self) -> str:
        if not self._on_widget_thread():
            return str(self._run_on_widget_thread(self.subtitle_ass_force_margins) or "no")
        value = self._player_property("sub-ass-force-margins", "no")
        return self._yes_no_value(value, "no")

    def set_subtitle_ass_force_margins(self, value: str) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(lambda: self.set_subtitle_ass_force_margins(value))
            return
        self._set_player_property("sub-ass-force-margins", self._yes_no_value(value, "no"))

    def supports_subtitle_scale(self) -> bool:
        if not self._on_widget_thread():
            return bool(self._run_on_widget_thread(self.supports_subtitle_scale))
        if self._player is None:
            return False
        try:
            _ = self._player["sub-scale"]
            return True
        except Exception as exc:
            if self._is_missing_mpv_property_error(exc):
                return False
            if getattr(self._player, "core_shutdown", False):
                return False
            raise

    def supports_secondary_subtitle_scale(self) -> bool:
        if not self._on_widget_thread():
            return bool(self._run_on_widget_thread(self.supports_secondary_subtitle_scale))
        if self._player is None:
            return False
        try:
            _ = self._player["secondary-sub-scale"]
            return True
        except Exception as exc:
            if self._is_missing_mpv_property_error(exc):
                return False
            if getattr(self._player, "core_shutdown", False):
                return False
            raise

    def supports_subtitle_ass_override(self) -> bool:
        if not self._on_widget_thread():
            return bool(self._run_on_widget_thread(self.supports_subtitle_ass_override))
        if self._player is None:
            return False
        try:
            _ = self._player["sub-ass-override"]
            return True
        except Exception as exc:
            if self._is_missing_mpv_property_error(exc):
                return False
            if getattr(self._player, "core_shutdown", False):
                return False
            raise

    def supports_secondary_subtitle_ass_override(self) -> bool:
        if not self._on_widget_thread():
            return bool(self._run_on_widget_thread(self.supports_secondary_subtitle_ass_override))
        if self._player is None:
            return False
        try:
            _ = self._player["secondary-sub-ass-override"]
            return True
        except Exception as exc:
            if self._is_missing_mpv_property_error(exc):
                return False
            if getattr(self._player, "core_shutdown", False):
                return False
            raise

    def supports_subtitle_ass_force_margins(self) -> bool:
        if not self._on_widget_thread():
            return bool(self._run_on_widget_thread(self.supports_subtitle_ass_force_margins))
        if self._player is None:
            return False
        try:
            _ = self._player["sub-ass-force-margins"]
            return True
        except Exception as exc:
            if self._is_missing_mpv_property_error(exc):
                return False
            if getattr(self._player, "core_shutdown", False):
                return False
            raise

    def _audio_language_label(self, lang: str) -> str:
        normalized = lang.strip().lower()
        return {
            "zh": "中文",
            "chi": "中文",
            "zho": "中文",
            "cmn": "国语",
            "en": "English",
            "eng": "English",
            "ja": "日语",
            "jpn": "日语",
        }.get(normalized, normalized or "")

    def _audio_track_label(self, title: str, lang: str, is_default: bool, is_forced: bool, index: int) -> str:
        base = title.strip() or self._audio_language_label(lang) or f"音轨 {index}"
        suffixes = []
        if is_default:
            suffixes.append("默认")
        if is_forced:
            suffixes.append("强制")
        if not suffixes:
            return base
        return f"{base} ({'/'.join(suffixes)})"

    def _audio_track_detail_label(self, raw_track: object, track_id: int) -> str:
        if not isinstance(raw_track, dict):
            return f"ID {track_id}"

        parts: list[str] = []

        codec = str(raw_track.get("codec") or raw_track.get("audio-codec") or "").strip().upper()
        if codec:
            parts.append(codec)

        channels = raw_track.get("audio-channels")
        if channels in (None, ""):
            channels = raw_track.get("channels")
        if channels not in (None, ""):
            parts.append(f"{channels}ch")

        samplerate = raw_track.get("audio-samplerate")
        if samplerate in (None, ""):
            samplerate = raw_track.get("samplerate")
        if samplerate not in (None, ""):
            parts.append(f"{samplerate}Hz")

        parts.append(f"ID {track_id}")
        return " / ".join(parts)

    def _audio_track_detail_parts(self, raw_track: object) -> list[str]:
        if not isinstance(raw_track, dict):
            return []

        parts: list[str] = []

        codec = str(raw_track.get("codec") or raw_track.get("audio-codec") or "").strip().upper()
        if codec:
            parts.append(codec)

        channels = raw_track.get("audio-channels")
        if channels in (None, ""):
            channels = raw_track.get("channels")
        if channels not in (None, ""):
            parts.append(f"{channels}ch")

        samplerate = raw_track.get("audio-samplerate")
        if samplerate in (None, ""):
            samplerate = raw_track.get("samplerate")
        if samplerate not in (None, ""):
            parts.append(f"{samplerate}Hz")

        return parts

    def _is_preferred_audio_track(self, track: AudioTrack) -> bool:
        if track.lang in {"zh", "chi", "zho", "cmn"}:
            return True
        lowered_title = track.title.casefold()
        return any(token in lowered_title for token in ("中文", "国语", "普通话", "mandarin", "chinese"))

    def _preferred_audio_sort_key(self, track: AudioTrack) -> tuple[int, int]:
        return (int(track.is_default), int(bool(track.title)))

    def audio_tracks(self) -> list[AudioTrack]:
        if not self._on_widget_thread():
            return list(self._run_on_widget_thread(self.audio_tracks) or [])
        if self._player is None:
            return []
        try:
            raw_tracks = getattr(self._player, "track_list", None) or []
        except Exception:
            return []

        track_entries: list[tuple[int, str, str, bool, bool, object]] = []
        for raw_track in raw_tracks:
            if raw_track.get("type") != "audio" or raw_track.get("external"):
                continue
            title = str(raw_track.get("title") or "").strip()
            lang = str(raw_track.get("lang") or "").strip().lower()
            is_default = bool(raw_track.get("default"))
            is_forced = bool(raw_track.get("forced"))
            track_entries.append(
                (
                    int(raw_track["id"]),
                    title,
                    lang,
                    is_default,
                    is_forced,
                    raw_track,
                )
            )

        base_labels = [
            self._audio_track_label(title, lang, is_default, is_forced, index + 1)
            for index, (_, title, lang, is_default, is_forced, _) in enumerate(track_entries)
        ]
        duplicate_labels = {label for label in base_labels if base_labels.count(label) > 1}

        tracks: list[AudioTrack] = []
        for index, (track_id, title, lang, is_default, is_forced, raw_track) in enumerate(track_entries):
            label = base_labels[index]
            detail_parts = self._audio_track_detail_parts(raw_track)
            if label in duplicate_labels:
                if detail_parts:
                    detail_parts.append(f"ID {track_id}")
                else:
                    detail_parts = [f"ID {track_id}"]
                label = f"{label} [{ ' / '.join(detail_parts) }]"
            tracks.append(
                AudioTrack(
                    id=track_id,
                    title=title,
                    lang=lang,
                    is_default=is_default,
                    is_forced=is_forced,
                    label=label,
                )
            )
        return tracks

    def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
        if not self._on_widget_thread():
            return self._run_on_widget_thread(lambda: self.apply_audio_mode(mode, track_id=track_id))
        if self._player is None:
            return None
        try:
            if mode == "track" and track_id is not None:
                self._player.aid = track_id
                return track_id
            # preferred_tracks = [track for track in self.audio_tracks() if self._is_preferred_audio_track(track)]
            # if preferred_tracks:
            #     preferred_track = max(preferred_tracks, key=self._preferred_audio_sort_key)
            #     self._player.aid = preferred_track.id
            #     return preferred_track.id
            self._player.aid = "auto"
            return None
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return None
            raise

    def toggle_video_info(self) -> None:
        if not self._on_widget_thread():
            self._post_to_widget_thread(self.toggle_video_info)
            return
        if self._player is None:
            return
        try:
            self._player.command("script-binding", "stats/display-stats-toggle")
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)
