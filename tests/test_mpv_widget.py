import sys
import types

from atv_player.player.mpv_widget import AudioTrack, MpvWidget, SubtitleTrack


class FakeDeadPlayer:
    core_shutdown = True


class FakeAlivePlayer:
    def __init__(self) -> None:
        self.play_calls: list[str] = []
        self.pause = False
        self.volume = 100
        self.mute = False
        self.options: dict[str, object] = {}

    def play(self, url: str) -> None:
        self.play_calls.append(url)

    def __setitem__(self, key: str, value: object) -> None:
        self.options[key] = value


def test_mpv_widget_recreates_player_when_core_is_shutdown(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = FakeDeadPlayer()

    alive = FakeAlivePlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: alive)

    widget.load("http://m/1.m3u8")

    assert widget._player is alive
    assert alive.play_calls == ["http://m/1.m3u8"]


def test_mpv_widget_reregisters_player_events_after_recreating_during_load_failure(
    qtbot,
    monkeypatch,
) -> None:
    class BrokenPlayer:
        def __init__(self) -> None:
            self.core_shutdown = False

        def play(self, url: str) -> None:
            self.core_shutdown = True
            raise RuntimeError(f"broken: {url}")

    class ReplacementPlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._end_file_callback = None
            self._track_list_observer = None

        def event_callback(self, *event_types):
            assert event_types == ("end-file",)

            def register(callback):
                self._end_file_callback = callback
                return callback

            return register

        def observe_property(self, name: str, handler) -> None:
            assert name == "track-list"
            self._track_list_observer = handler

        def play(self, url: str) -> None:
            self.play_calls.append(url)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = BrokenPlayer()
    replacement = ReplacementPlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: replacement)

    finished = {"count": 0}
    subtitle_changes = {"count": 0}
    audio_changes = {"count": 0}
    widget.playback_finished.connect(lambda: finished.__setitem__("count", finished["count"] + 1))
    widget.subtitle_tracks_changed.connect(
        lambda: subtitle_changes.__setitem__("count", subtitle_changes["count"] + 1)
    )
    widget.audio_tracks_changed.connect(
        lambda: audio_changes.__setitem__("count", audio_changes["count"] + 1)
    )

    widget.load("http://m/1.m3u8")

    replacement._end_file_callback(types.SimpleNamespace(data=types.SimpleNamespace(reason=0)))
    replacement._track_list_observer("track-list", [{"id": 1, "type": "sub"}])

    assert widget._player is replacement
    assert replacement.play_calls == ["http://m/1.m3u8"]
    assert finished["count"] == 1
    assert subtitle_changes["count"] == 1
    assert audio_changes["count"] == 1


def test_mpv_widget_updates_volume_and_mute_state(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = FakeAlivePlayer()

    widget.set_volume(35)
    widget.toggle_mute()
    widget.toggle_mute()

    assert widget._player.volume == 35
    assert widget._player.mute is False


def test_mpv_widget_sets_http_header_fields_as_property_before_loading(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.calls: list[tuple[str, str, dict[str, object]]] = []
            self.options: dict[str, object] = {}

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.calls.append((url, mode, options))

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    widget.load(
        "http://m/1.m3u8",
        headers={
            "User-Agent": "Yamby/1.5.7.18(Android",
            "Referer": "https://site.example",
        },
    )

    assert widget._player.calls == [
        ("http://m/1.m3u8", "replace", {})
    ]
    assert widget._player.options == {
        "http-header-fields": [
            "User-Agent: Yamby/1.5.7.18(Android",
            "Referer: https://site.example",
        ]
    }


def test_mpv_widget_clears_previous_http_header_fields_when_loading_without_headers(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.play_calls: list[str] = []
            self.loadfile_calls: list[str] = []
            self.options: dict[str, object] = {}

        def play(self, url: str) -> None:
            self.play_calls.append(url)

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.loadfile_calls.append(url)

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    widget.load("http://m/1.m3u8", headers={"Referer": "https://site.example"})
    widget.load("http://m/2.m3u8")

    assert widget._player.loadfile_calls == ["http://m/1.m3u8"]
    assert widget._player.play_calls == ["http://m/2.m3u8"]
    assert widget._player.options == {
        "http-header-fields": []
    }


def test_mpv_widget_updates_native_cursor_autohide_property(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = FakeAlivePlayer()

    widget.set_cursor_autohide(3000)
    widget.set_cursor_autohide(None)

    assert widget._player.options == {
        "cursor-autohide": "no",
        "cursor-autohide-fs-only": False,
        "input-cursor": True,
    }


def test_mpv_widget_close_terminates_active_player(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    terminated = {"count": 0}

    class FakePlayer:
        core_shutdown = False

        def terminate(self) -> None:
            terminated["count"] += 1

    widget._player = FakePlayer()
    widget.show()

    widget.close()

    assert terminated["count"] == 1
    assert widget._player is None


def test_mpv_widget_disables_mpv_keyboard_bindings_for_embedded_player(qtbot, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setitem(sys.modules, "mpv", types.SimpleNamespace(MPV=FakeMPV))

    widget._create_player()

    assert captured["input_default_bindings"] is False
    assert captured["input_vo_keyboard"] is False


def test_mpv_widget_emits_playback_finished_only_for_natural_end(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._end_file_callback = None

        def event_callback(self, *event_types):
            assert event_types == ("end-file",)

            def register(callback):
                self._end_file_callback = callback
                return callback

            return register

        def play(self, url: str) -> None:
            self.play_calls.append(url)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakePlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: player)
    finished = {"count": 0}
    widget.playback_finished.connect(lambda: finished.__setitem__("count", finished["count"] + 1))

    widget.load("http://m/1.m3u8")
    player._end_file_callback(types.SimpleNamespace(data=types.SimpleNamespace(reason=0)))
    player._end_file_callback(types.SimpleNamespace(data=types.SimpleNamespace(reason=2)))

    assert player.play_calls == ["http://m/1.m3u8"]
    assert finished["count"] == 1


def test_mpv_widget_emits_playback_failed_with_reason_from_end_file_event(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._end_file_callback = None

        def event_callback(self, *event_types):
            assert event_types == ("end-file",)

            def register(callback):
                self._end_file_callback = callback
                return callback

            return register

        def play(self, url: str) -> None:
            self.play_calls.append(url)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakePlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: player)
    failures: list[str] = []
    widget.playback_failed.connect(failures.append)

    widget.load("http://m/1.m3u8")
    player._end_file_callback(
        types.SimpleNamespace(
            data=types.SimpleNamespace(
                reason=4,
                error="HTTP 403 Forbidden",
            )
        )
    )

    assert failures == ["播放失败: HTTP 403 Forbidden"]


def test_mpv_widget_does_not_treat_aborted_end_file_as_failure(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._end_file_callback = None

        def event_callback(self, *event_types):
            assert event_types == ("end-file",)

            def register(callback):
                self._end_file_callback = callback
                return callback

            return register

        def play(self, url: str) -> None:
            self.play_calls.append(url)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakePlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: player)
    failures: list[str] = []
    widget.playback_failed.connect(failures.append)

    widget.load("http://m/1.m3u8")
    player._end_file_callback(types.SimpleNamespace(data=types.SimpleNamespace(reason=2, error="")))

    assert failures == []


def test_mpv_widget_emits_playback_failed_with_unknown_error_fallback(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._end_file_callback = None

        def event_callback(self, *event_types):
            assert event_types == ("end-file",)

            def register(callback):
                self._end_file_callback = callback
                return callback

            return register

        def play(self, url: str) -> None:
            self.play_calls.append(url)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakePlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: player)
    failures: list[str] = []
    widget.playback_failed.connect(failures.append)

    widget.load("http://m/1.m3u8")
    player._end_file_callback(types.SimpleNamespace(data=types.SimpleNamespace(reason=4, error="")))

    assert failures == ["播放失败: 未知错误"]


def test_mpv_widget_registers_right_click_binding_and_emits_context_menu_requested(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._right_click_handler = None
            self._left_click_handler = None

        def event_callback(self, *_event_types):
            def register(callback):
                return callback

            return register

        def observe_property(self, _name: str, _handler) -> None:
            return None

        def register_key_binding(self, keydef: str, callback, mode: str = "force") -> None:
            assert mode == "force"
            if keydef == "MBTN_RIGHT":
                self._right_click_handler = callback
                return
            if keydef == "MBTN_LEFT":
                self._left_click_handler = callback
                return
            raise AssertionError(keydef)

        def play(self, url: str) -> None:
            self.play_calls.append(url)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakePlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: player)
    opened = {"count": 0}
    widget.context_menu_requested.connect(lambda: opened.__setitem__("count", opened["count"] + 1))

    widget.load("http://m/1.m3u8")

    assert player.play_calls == ["http://m/1.m3u8"]
    assert player._right_click_handler is not None
    assert player._left_click_handler is not None

    player._right_click_handler("d", "MBTN_RIGHT", None, None, None)

    assert opened["count"] == 1


def test_mpv_widget_registers_left_click_binding_and_emits_context_menu_dismiss_requested(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._right_click_handler = None
            self._left_click_handler = None

        def event_callback(self, *_event_types):
            def register(callback):
                return callback

            return register

        def observe_property(self, _name: str, _handler) -> None:
            return None

        def register_key_binding(self, keydef: str, callback, mode: str = "force") -> None:
            assert mode == "force"
            if keydef == "MBTN_RIGHT":
                self._right_click_handler = callback
                return
            if keydef == "MBTN_LEFT":
                self._left_click_handler = callback
                return
            raise AssertionError(keydef)

        def play(self, url: str) -> None:
            self.play_calls.append(url)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakePlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: player)
    dismissed = {"count": 0}
    widget.context_menu_dismiss_requested.connect(lambda: dismissed.__setitem__("count", dismissed["count"] + 1))

    widget.load("http://m/1.m3u8")

    assert player.play_calls == ["http://m/1.m3u8"]
    assert player._left_click_handler is not None

    player._left_click_handler("d", "MBTN_LEFT", None, None, None)

    assert dismissed["count"] == 1


def test_mpv_widget_emits_subtitle_tracks_changed_when_mpv_track_list_updates(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._track_list_observer = None

        def event_callback(self, *event_types):
            def register(callback):
                return callback

            return register

        def observe_property(self, name: str, handler) -> None:
            assert name == "track-list"
            self._track_list_observer = handler

        def play(self, url: str) -> None:
            self.play_calls.append(url)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakePlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: player)
    changes = {"count": 0}
    widget.subtitle_tracks_changed.connect(lambda: changes.__setitem__("count", changes["count"] + 1))

    widget.load("http://m/1.m3u8")
    player._track_list_observer("track-list", [{"id": 1, "type": "sub"}])

    assert player.play_calls == ["http://m/1.m3u8"]
    assert changes["count"] == 1


def test_mpv_widget_lists_embedded_subtitle_tracks_with_readable_labels(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = types.SimpleNamespace(
        track_list=[
            {"id": 1, "type": "sub", "lang": "zh-hans", "title": "", "default": True, "forced": False, "external": False},
            {"id": 2, "type": "sub", "lang": "zh-hant", "title": "", "default": False, "forced": False, "external": False},
            {"id": 3, "type": "sub", "lang": "eng", "title": "Signs", "default": False, "forced": True, "external": False},
            {"id": 3, "type": "audio", "lang": "ja", "title": "", "default": False, "forced": False, "external": False},
            {"id": 4, "type": "sub", "lang": "zho", "title": "外挂", "default": False, "forced": False, "external": True},
        ]
    )

    assert widget.subtitle_tracks() == [
        SubtitleTrack(id=1, title="", lang="zh-hans", is_default=True, is_forced=False, label="简体中文 (默认)"),
        SubtitleTrack(id=2, title="", lang="zh-hant", is_default=False, is_forced=False, label="繁体中文"),
        SubtitleTrack(id=3, title="Signs", lang="eng", is_default=False, is_forced=True, label="Signs (强制)"),
    ]


def test_mpv_widget_auto_mode_prefers_chinese_embedded_subtitles(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        sid="auto",
        track_list=[
            {"id": 3, "type": "sub", "lang": "eng", "title": "English", "default": False, "forced": False, "external": False},
            {"id": 5, "type": "sub", "lang": "chi", "title": "", "default": True, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_subtitle_mode("auto")

    assert applied_track_id == 5
    assert player.sid == 5


def test_mpv_widget_auto_mode_prefers_simplified_chinese_over_traditional(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        sid="auto",
        track_list=[
            {"id": 5, "type": "sub", "lang": "zh", "title": "繁中", "default": True, "forced": False, "external": False},
            {"id": 6, "type": "sub", "lang": "zh", "title": "简中", "default": False, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_subtitle_mode("auto")

    assert applied_track_id == 6
    assert player.sid == 6


def test_mpv_widget_auto_mode_recognizes_simplified_and_traditional_english_titles(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        sid="auto",
        track_list=[
            {"id": 5, "type": "sub", "lang": "", "title": "Traditional Chinese", "default": True, "forced": False, "external": False},
            {"id": 6, "type": "sub", "lang": "", "title": "Simplified Chinese", "default": False, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_subtitle_mode("auto")

    assert applied_track_id == 6
    assert player.sid == 6


def test_mpv_widget_auto_mode_falls_back_to_mpv_default_without_chinese_or_english_tracks(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        sid=7,
        track_list=[
            {"id": 7, "type": "sub", "lang": "jpn", "title": "Japanese", "default": False, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_subtitle_mode("auto")

    assert applied_track_id is None
    assert player.sid == "auto"


def test_mpv_widget_auto_mode_prefers_english_when_chinese_tracks_are_absent(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        sid="auto",
        track_list=[
            {"id": 7, "type": "sub", "lang": "jpn", "title": "Japanese", "default": False, "forced": False, "external": False},
            {"id": 9, "type": "sub", "lang": "eng", "title": "English", "default": True, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_subtitle_mode("auto")

    assert applied_track_id == 9
    assert player.sid == 9


def test_mpv_widget_can_disable_or_select_a_specific_embedded_subtitle_track(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(sid="auto", track_list=[])
    widget._player = player

    disabled_track_id = widget.apply_subtitle_mode("off")
    selected_track_id = widget.apply_subtitle_mode("track", track_id=9)

    assert disabled_track_id is None
    assert selected_track_id == 9
    assert player.sid == 9


def test_mpv_widget_can_disable_or_select_a_specific_secondary_subtitle_track(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(track_list=[])
    widget._player = player

    disabled_track_id = widget.apply_secondary_subtitle_mode("off")
    selected_track_id = widget.apply_secondary_subtitle_mode("track", track_id=12)

    assert disabled_track_id is None
    assert selected_track_id == 12
    assert player.secondary_sid == 12


def test_mpv_widget_reads_and_writes_primary_subtitle_position(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"sub-pos": 50}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.subtitle_position() == 50

    widget.set_subtitle_position(70)

    assert widget.subtitle_position() == 70


def test_mpv_widget_reads_and_writes_secondary_subtitle_position(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"secondary-sub-pos": 50}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.secondary_subtitle_position() == 50

    widget.set_secondary_subtitle_position(30)

    assert widget.secondary_subtitle_position() == 30


def test_mpv_widget_reports_secondary_subtitle_position_unsupported_when_property_is_missing(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __getitem__(self, key: str) -> object:
            raise RuntimeError(("mpv property does not exist", -8, (object(), b"options/secondary-sub-pos", b"50")))

    widget._player = FakePlayer()

    assert widget.supports_secondary_subtitle_position() is False


def test_mpv_widget_reads_and_writes_primary_subtitle_scale(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"sub-scale": 1.0}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.subtitle_scale() == 100

    widget.set_subtitle_scale(115)

    assert widget.subtitle_scale() == 115


def test_mpv_widget_reads_and_writes_secondary_subtitle_scale(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"secondary-sub-scale": 1.0}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.secondary_subtitle_scale() == 100

    widget.set_secondary_subtitle_scale(130)

    assert widget.secondary_subtitle_scale() == 130


def test_mpv_widget_reports_primary_subtitle_scale_unsupported_when_property_is_missing(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __getitem__(self, key: str) -> object:
            raise RuntimeError(("mpv property does not exist", -8, (object(), b"options/sub-scale", b"1.0")))

    widget._player = FakePlayer()

    assert widget.supports_subtitle_scale() is False


def test_mpv_widget_reports_secondary_subtitle_scale_unsupported_when_property_is_missing(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __getitem__(self, key: str) -> object:
            raise RuntimeError(("mpv property does not exist", -8, (object(), b"options/secondary-sub-scale", b"1.0")))

    widget._player = FakePlayer()

    assert widget.supports_secondary_subtitle_scale() is False


def test_mpv_widget_emits_audio_tracks_changed_when_mpv_track_list_updates(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._track_list_observer = None

        def event_callback(self, *event_types):
            def register(callback):
                return callback

            return register

        def observe_property(self, name: str, handler) -> None:
            assert name == "track-list"
            self._track_list_observer = handler

        def play(self, url: str) -> None:
            self.play_calls.append(url)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakePlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: player)
    changes = {"count": 0}
    widget.audio_tracks_changed.connect(lambda: changes.__setitem__("count", changes["count"] + 1))

    widget.load("http://m/1.m3u8")
    player._track_list_observer("track-list", [{"id": 1, "type": "audio"}])

    assert player.play_calls == ["http://m/1.m3u8"]
    assert changes["count"] == 1


def test_mpv_widget_lists_embedded_audio_tracks_with_readable_labels(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = types.SimpleNamespace(
        track_list=[
            {"id": 1, "type": "audio", "lang": "cmn", "title": "", "default": True, "forced": False, "external": False},
            {"id": 2, "type": "audio", "lang": "eng", "title": "English Dub", "default": False, "forced": False, "external": False},
            {"id": 3, "type": "sub", "lang": "zh", "title": "", "default": True, "forced": False, "external": False},
            {"id": 4, "type": "audio", "lang": "jpn", "title": "", "default": False, "forced": False, "external": True},
        ]
    )

    assert widget.audio_tracks() == [
        AudioTrack(id=1, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
        AudioTrack(id=2, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
    ]


def test_mpv_widget_auto_mode_prefers_chinese_or_mandarin_audio(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        aid="auto",
        track_list=[
            {"id": 3, "type": "audio", "lang": "eng", "title": "English", "default": True, "forced": False, "external": False},
            {"id": 5, "type": "audio", "lang": "cmn", "title": "", "default": False, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_audio_mode("auto")

    assert applied_track_id == 5
    assert player.aid == 5


def test_mpv_widget_auto_mode_falls_back_to_mpv_default_without_preferred_audio(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        aid=7,
        track_list=[
            {"id": 7, "type": "audio", "lang": "eng", "title": "English", "default": False, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_audio_mode("auto")

    assert applied_track_id is None
    assert player.aid == "auto"


def test_mpv_widget_can_select_a_specific_embedded_audio_track(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(aid="auto", track_list=[])
    widget._player = player

    selected_track_id = widget.apply_audio_mode("track", track_id=9)

    assert selected_track_id == 9
    assert player.aid == 9
