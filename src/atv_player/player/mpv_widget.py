from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QMouseEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


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
    subtitle_tracks_changed = Signal()
    audio_tracks_changed = Signal()

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

        return mpv.MPV(
            wid=str(int(self.winId())),
            input_default_bindings=False,
            input_vo_keyboard=False,
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
            eof_reason = getattr(type(event_data), "EOF", 0)
            if getattr(event_data, "reason", None) == eof_reason:
                self.playback_finished.emit()

        self._end_file_handler = handle_end_file
        observe_property = getattr(self._player, "observe_property", None)
        if observe_property is None:
            return

        def handle_track_list(_property_name, _tracks) -> None:
            self.subtitle_tracks_changed.emit()
            self.audio_tracks_changed.emit()

        observe_property("track-list", handle_track_list)
        self._track_list_handler = handle_track_list

    def _build_http_header_fields(self, headers: dict[str, str] | None) -> list[str]:
        if not headers:
            return []
        return [f"{key}: {value}" for key, value in headers.items()]

    def _apply_http_header_fields(self, player: Any, header_fields: list[str]) -> None:
        if not hasattr(type(player), "__setitem__"):
            return
        player["http-header-fields"] = header_fields

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
        try:
            self._apply_http_header_fields(player, header_fields)
            if start_seconds > 0:
                player.loadfile(url, start=str(start_seconds))
            elif header_fields:
                player.loadfile(url)
            else:
                player.play(url)
        except Exception:
            if getattr(player, "core_shutdown", False):
                player = self._create_player()
                self._player = player
                self._register_player_events()
                self._apply_http_header_fields(player, header_fields)
                if start_seconds > 0:
                    player.loadfile(url, start=str(start_seconds))
                elif header_fields:
                    player.loadfile(url)
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

    def subtitle_position(self) -> int:
        value = self._player_property("sub-pos", 50)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 50

    def set_subtitle_position(self, value: int) -> None:
        clamped = max(0, min(int(value), 100))
        self._set_player_property("sub-pos", clamped)

    def secondary_subtitle_position(self) -> int:
        value = self._player_property("secondary-sub-pos", 50)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 50

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
        try:
            return int(round(float(value) * 100))
        except (TypeError, ValueError):
            return 100

    def set_subtitle_scale(self, value: int) -> None:
        clamped = max(50, min(int(value), 200))
        self._set_player_property("sub-scale", clamped / 100)

    def secondary_subtitle_scale(self) -> int:
        value = self._player_property("secondary-sub-scale", 1.0)
        try:
            return int(round(float(value) * 100))
        except (TypeError, ValueError):
            return 100

    def set_secondary_subtitle_scale(self, value: int) -> None:
        clamped = max(50, min(int(value), 200))
        self._set_player_property("secondary-sub-scale", clamped / 100)

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
            preferred_tracks = [track for track in self.audio_tracks() if self._is_preferred_audio_track(track)]
            if preferred_tracks:
                preferred_track = max(preferred_tracks, key=self._preferred_audio_sort_key)
                self._player.aid = preferred_track.id
                return preferred_track.id
            self._player.aid = "auto"
            return None
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return None
            raise

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)
