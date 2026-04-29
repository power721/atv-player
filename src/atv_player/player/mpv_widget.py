from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QMouseEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

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


class MpvWidget(QWidget):
    double_clicked = Signal()
    playback_finished = Signal()
    playback_failed = Signal(str)
    subtitle_tracks_changed = Signal()
    audio_tracks_changed = Signal()
    context_menu_requested = Signal()
    context_menu_dismiss_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._player: Any | None = None
        self._placeholder = QLabel("")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout = QVBoxLayout(self)
        layout.addWidget(self._placeholder)

    def _create_player(self):
        import mpv

        common = dict(
            wid=str(int(self.winId())),
            hwdec="auto-safe",
            audio_spdif="no",
            ad="ffmpeg",
            input_default_bindings=False,
            input_vo_keyboard=False,
            cache=True,
            cache_pause_initial=True,
            cache_pause_wait=3,
            demuxer_max_bytes="512M",
            demuxer_max_back_bytes="128M",
            demuxer_readahead_secs=20,
            stream_buffer_size="4M",
            network_timeout=15,
        )
        if os.getenv("ATV_MPV_DEBUG"):
            common["log_handler"] = print
            common["loglevel"] = "warn"

        if sys.platform.startswith("win"):
            return mpv.MPV(
                **common,
                audio_device="auto",
                audio_exclusive="no",
            )

        elif sys.platform == "darwin":
            return mpv.MPV(
                **common,
                # macOS 👉 不指定最稳
                # audio_device="auto" 也可以
                audio_exclusive="no",
            )

        else:
            return mpv.MPV(
                **common,
                ao="pulse,pipewire,alsa,",
            )

    def _ensure_player(self) -> None:
        if self._player is not None and not getattr(self._player, "core_shutdown", False):
            return
        self._player = self._create_player()
        self._register_player_events()

    def shutdown(self) -> None:
        if self._player is None:
            return
        player, self._player = self._player, None
        if getattr(player, "core_shutdown", False):
            return
        try:
            terminate = getattr(player, "terminate", None)
            if terminate is not None:
                terminate()
        except Exception:
            if getattr(player, "core_shutdown", False):
                return
            raise

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
            if reason == eof_reason:
                self.playback_finished.emit()
                return
            error_reason = getattr(type(event_data), "ERROR", 4)
            if reason == error_reason:
                self.playback_failed.emit(self._format_end_file_failure_message(event_data))

        self._end_file_handler = handle_end_file
        observe_property = getattr(self._player, "observe_property", None)
        if observe_property is None:
            return

        def handle_track_list(_property_name, _tracks) -> None:
            self.subtitle_tracks_changed.emit()
            self.audio_tracks_changed.emit()

        observe_property("track-list", handle_track_list)
        self._track_list_handler = handle_track_list

        register_key_binding = getattr(self._player, "register_key_binding", None)
        if register_key_binding is None:
            return

        def handle_right_click(*_args) -> None:
            self.context_menu_requested.emit()

        def handle_left_click(*_args) -> None:
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

    def _loadfile_options(self, url: str) -> dict[str, str]:
        if ".m3u8" not in url.lower():
            return {}
        # Some HLS sources disguise transport stream segments with image suffixes.
        return {"demuxer_lavf_o_add": "allowed_extensions=ALL"}

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
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

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
    ) -> None:
        self._ensure_player()
        player = self._player
        if player is None:
            return
        header_fields = self._build_http_header_fields(headers)
        loadfile_options = self._loadfile_options(url)
        can_loadfile = hasattr(player, "loadfile")
        try:
            self._apply_http_header_fields(player, header_fields)
            if start_seconds > 0 and can_loadfile:
                player.loadfile(url, start=str(start_seconds), **loadfile_options)
            elif (header_fields or loadfile_options) and can_loadfile:
                player.loadfile(url, **loadfile_options)
            else:
                player.play(url)
        except Exception:
            if getattr(player, "core_shutdown", False):
                player = self._create_player()
                self._player = player
                self._register_player_events()
                self._apply_http_header_fields(player, header_fields)
                can_loadfile = hasattr(player, "loadfile")
                if start_seconds > 0 and can_loadfile:
                    player.loadfile(url, start=str(start_seconds), **loadfile_options)
                elif (header_fields or loadfile_options) and can_loadfile:
                    player.loadfile(url, **loadfile_options)
                else:
                    player.play(url)
            else:
                raise
        player.pause = pause

    def seek(self, seconds: int) -> None:
        if self._player is None:
            return
        try:
            self._player.command("seek", seconds, "absolute")
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def seek_relative(self, seconds: int) -> None:
        if self._player is None:
            return
        try:
            self._player.command("seek", seconds, "relative")
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def can_seek(self) -> bool:
        if self._player is None:
            return False
        try:
            return bool(self._player.seekable)
        except Exception:
            return False

    def set_speed(self, speed: float) -> None:
        if self._player is None:
            return
        try:
            self._player.speed = speed
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def set_volume(self, value: int) -> None:
        if self._player is None:
            return
        try:
            self._player.volume = value
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def toggle_mute(self) -> None:
        if self._player is None:
            return
        try:
            self._player.mute = not bool(getattr(self._player, "mute", False))
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def set_muted(self, muted: bool) -> None:
        if self._player is None:
            return
        try:
            self._player.mute = muted
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def set_cursor_autohide(self, value: int | None) -> None:
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
        if self._player is None:
            return
        try:
            self._player.pause = True
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def resume(self) -> None:
        if self._player is None:
            return
        try:
            self._player.pause = False
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def position_seconds(self) -> int | None:
        if self._player is None:
            return None
        try:
            pos = self._player.time_pos
            return int(pos) if pos is not None else None
        except Exception:
            return None

    def duration_seconds(self) -> int:
        if self._player is None:
            return 0
        try:
            return int(self._player.duration or 0)
        except Exception:
            return 0

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
        base = title.strip() or self._subtitle_language_label(lang) or f"字幕 {index}"
        suffixes = []
        if is_default:
            suffixes.append("默认")
        if is_forced:
            suffixes.append("强制")
        if not suffixes:
            return base
        return f"{base} ({'/'.join(suffixes)})"

    def _is_chinese_subtitle_track(self, track: SubtitleTrack) -> bool:
        if track.lang in {"zh", "chi", "zho", "chs", "zh-cn", "zh-hans", "zh-tw", "cht", "zh-hant"}:
            return True
        lowered_title = track.title.casefold()
        return any(token in lowered_title for token in ("中文", "简中", "繁中", "中字", "chinese"))

    def _chinese_subtitle_preference(self, track: SubtitleTrack) -> int:
        normalized_lang = track.lang.casefold()
        lowered_title = track.title.casefold()
        simplified_langs = {"zh", "chi", "zho", "chs", "zh-cn", "zh-hans"}
        traditional_langs = {"zh-tw", "cht", "zh-hant"}
        simplified_tokens = ("简中", "简体", "chs", "sc", "gb", "hans", "simplified")
        traditional_tokens = ("繁中", "繁體", "繁体", "cht", "tc", "big5", "hant", "traditional", "tranditional")
        if any(token in lowered_title for token in simplified_tokens):
            return 2
        if any(token in lowered_title for token in traditional_tokens):
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
        if self._player is None:
            return []
        try:
            raw_tracks = getattr(self._player, "track_list", None) or []
        except Exception:
            return []

        tracks: list[SubtitleTrack] = []
        for raw_track in raw_tracks:
            if raw_track.get("type") != "sub" or raw_track.get("external"):
                continue
            title = str(raw_track.get("title") or "").strip()
            lang = str(raw_track.get("lang") or "").strip().lower()
            is_default = bool(raw_track.get("default"))
            is_forced = bool(raw_track.get("forced"))
            tracks.append(
                SubtitleTrack(
                    id=int(raw_track["id"]),
                    title=title,
                    lang=lang,
                    is_default=is_default,
                    is_forced=is_forced,
                    label=self._subtitle_track_label(title, lang, is_default, is_forced, len(tracks) + 1),
                )
            )
        return tracks

    def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
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

    def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
        if self._player is None:
            return None
        before_ids = self._subtitle_track_ids()
        try:
            self._player.command("sub-add", path, "auto")
            after_ids = self._subtitle_track_ids()
            new_ids = sorted(after_ids - before_ids)
            track_id = new_ids[-1] if new_ids else None
            if select_for_secondary and track_id is not None:
                self.apply_secondary_subtitle_mode("track", track_id=track_id)
            return track_id
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return None
            raise

    def remove_subtitle_track(self, track_id: int | None) -> None:
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
        value = self._player_property("sub-pos", 50)
        return self._int_property_value(value, 50)

    def set_subtitle_position(self, value: int) -> None:
        clamped = max(0, min(int(value), 100))
        self._set_player_property("sub-pos", clamped)

    def secondary_subtitle_position(self) -> int:
        value = self._player_property("secondary-sub-pos", 50)
        return self._int_property_value(value, 50)

    def supports_secondary_subtitle_position(self) -> bool:
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
        clamped = max(0, min(int(value), 100))
        self._set_player_property("secondary-sub-pos", clamped)

    def subtitle_scale(self) -> int:
        value = self._player_property("sub-scale", 1.0)
        return self._scale_property_percent(value, 100)

    def set_subtitle_scale(self, value: int) -> None:
        clamped = max(50, min(int(value), 200))
        self._set_player_property("sub-scale", clamped / 100)

    def secondary_subtitle_scale(self) -> int:
        value = self._player_property("secondary-sub-scale", 1.0)
        return self._scale_property_percent(value, 100)

    def set_secondary_subtitle_scale(self, value: int) -> None:
        clamped = max(50, min(int(value), 200))
        self._set_player_property("secondary-sub-scale", clamped / 100)

    def subtitle_ass_override(self) -> str:
        value = self._player_property("sub-ass-override", "scale")
        return self._ass_override_value(value, "scale")

    def set_subtitle_ass_override(self, value: str) -> None:
        self._set_player_property("sub-ass-override", self._ass_override_value(value, "scale"))

    def secondary_subtitle_ass_override(self) -> str:
        value = self._player_property("secondary-sub-ass-override", "strip")
        return self._ass_override_value(value, "strip")

    def set_secondary_subtitle_ass_override(self, value: str) -> None:
        self._set_player_property("secondary-sub-ass-override", self._ass_override_value(value, "strip"))

    def subtitle_ass_force_margins(self) -> str:
        value = self._player_property("sub-ass-force-margins", "no")
        return self._yes_no_value(value, "no")

    def set_subtitle_ass_force_margins(self, value: str) -> None:
        self._set_player_property("sub-ass-force-margins", self._yes_no_value(value, "no"))

    def supports_subtitle_scale(self) -> bool:
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

    def _is_preferred_audio_track(self, track: AudioTrack) -> bool:
        if track.lang in {"zh", "chi", "zho", "cmn"}:
            return True
        lowered_title = track.title.casefold()
        return any(token in lowered_title for token in ("中文", "国语", "普通话", "mandarin", "chinese"))

    def _preferred_audio_sort_key(self, track: AudioTrack) -> tuple[int, int]:
        return (int(track.is_default), int(bool(track.title)))

    def audio_tracks(self) -> list[AudioTrack]:
        if self._player is None:
            return []
        try:
            raw_tracks = getattr(self._player, "track_list", None) or []
        except Exception:
            return []

        tracks: list[AudioTrack] = []
        for raw_track in raw_tracks:
            if raw_track.get("type") != "audio" or raw_track.get("external"):
                continue
            title = str(raw_track.get("title") or "").strip()
            lang = str(raw_track.get("lang") or "").strip().lower()
            is_default = bool(raw_track.get("default"))
            is_forced = bool(raw_track.get("forced"))
            tracks.append(
                AudioTrack(
                    id=int(raw_track["id"]),
                    title=title,
                    lang=lang,
                    is_default=is_default,
                    is_forced=is_forced,
                    label=self._audio_track_label(title, lang, is_default, is_forced, len(tracks) + 1),
                )
            )
        return tracks

    def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
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
