from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    id: int
    title: str
    lang: str
    is_default: bool
    is_forced: bool
    label: str


class MpvWidget(QWidget):
    double_clicked = Signal()
    playback_finished = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._player = None
        self._placeholder = QLabel("mpv surface")
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

    def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
        self._ensure_player()
        try:
            if start_seconds > 0:
                self._player.loadfile(url, start=str(start_seconds))
            else:
                self._player.play(url)
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                self._player = self._create_player()
                if start_seconds > 0:
                    self._player.loadfile(url, start=str(start_seconds))
                else:
                    self._player.play(url)
            else:
                raise
        self._player.pause = pause

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

    def position_seconds(self) -> int:
        if self._player is None:
            return 0
        try:
            return int(self._player.time_pos or 0)
        except Exception:
            return 0

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
            "zh": "中文",
            "chi": "中文",
            "zho": "中文",
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
        if track.lang in {"zh", "chi", "zho"}:
            return True
        lowered_title = track.title.casefold()
        return any(token in lowered_title for token in ("中文", "简中", "繁中", "中字", "chinese"))

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
            preferred_track = next((track for track in self.subtitle_tracks() if self._is_chinese_subtitle_track(track)), None)
            if preferred_track is not None:
                self._player.sid = preferred_track.id
                return preferred_track.id
            self._player.sid = "auto"
            return None
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return None
            raise

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)
