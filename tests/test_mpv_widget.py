import sys
import threading
import time
import types

import pytest

from atv_player.player import mpv_widget as mpv_widget_module
from atv_player.models import AppConfig
from atv_player.player.mpv_user_config import ShaderPreset
from atv_player.player.mpv_widget import (
    _RUNTIME_OBSERVED_PROPERTIES,
    AudioTrack,
    Chapter,
    MpvWidget,
    SubtitleTrack,
)


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


class FakeRuntimePlayer:
    core_shutdown = False


def _fill_runtime_observed_properties(widget: MpvWidget) -> None:
    """模拟 observe_property 已推送的持续项值,供 snapshot/读取测试直接消费。"""
    widget._observed_properties.update(
        {
            "frame-drop-count": 3,
            "cache-buffering-state": 100,
            "video-bitrate": None,
            "audio-bitrate": 69533.0,
            "audio-codec": "AAC (Advanced Audio Coding)",
            "demuxer-cache-state": {"raw-input-rate": 7_201_569, "cache-duration": 3.64},
        }
    )


def test_mpv_widget_telemetry_snapshot_combines_cached_and_runtime_values(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = FakeRuntimePlayer()
    widget._telemetry = {
        "video-params": {"w": 1920, "h": 1080},
        "video-format": "h264",
        "hwdec-current": "nvdec",
        "container-fps": 23.976,
    }
    _fill_runtime_observed_properties(widget)

    snapshot = widget.telemetry_snapshot()

    assert snapshot["video_width"] == 1920
    assert snapshot["video_height"] == 1080
    assert snapshot["video_format"] == "h264"
    assert snapshot["hwdec_current"] == "nvdec"
    assert snapshot["container_fps"] == 23.976
    assert snapshot["frame_drop_count"] == 3
    assert snapshot["cache_buffering_state"] == 100
    assert snapshot["video_bitrate"] is None
    assert snapshot["audio_bitrate"] == 69533.0
    assert snapshot["audio_codec"] == "AAC (Advanced Audio Coding)"
    assert snapshot["input_rate_bytes"] == 7_201_569
    assert snapshot["cache_duration"] == 3.64


def test_mpv_widget_telemetry_snapshot_returns_empty_without_player(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = FakeDeadPlayer()

    assert widget.telemetry_snapshot() == {}


def test_mpv_widget_telemetry_snapshot_tolerates_missing_runtime_attrs(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class BarePlayer:
        core_shutdown = False

    widget._player = BarePlayer()

    snapshot = widget.telemetry_snapshot()

    assert snapshot == {
        "frame_drop_count": None,
        "cache_buffering_state": None,
        "video_bitrate": None,
        "audio_bitrate": None,
        "audio_codec": None,
    }


def test_mpv_widget_position_seconds_reads_observed_property_cache(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._observed_properties["time-pos"] = 91.4

    assert widget.position_seconds() == 91


def test_mpv_widget_position_seconds_returns_none_without_cache(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    # 非数字/缺失一律 None,不落回同步 get(那是 GUI 冻结根因)
    widget._observed_properties["time-pos"] = None

    assert widget.position_seconds() is None


def test_mpv_widget_runtime_property_cache_cleared_on_load(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._observed_properties.update({"time-pos": 12.0, "duration": 300.0})

    class FakeLoadPlayer:
        core_shutdown = False
        pause = False
        loadfile_calls: list[tuple[str, dict[str, object]]] = []
        options: dict[str, object] = {}

        def event_callback(self, *event_types):
            def register(callback):
                return callback

            return register

        def observe_property(self, name: str, handler) -> None:
            return None

        def loadfile(self, url: str, options: dict[str, object]) -> None:
            self.loadfile_calls.append((url, options))

        def play(self, url: str) -> None:
            return None

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    player = FakeLoadPlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: player)

    widget.load("http://m/1.mp4")

    assert "time-pos" not in widget._observed_properties
    assert "duration" not in widget._observed_properties


def test_mpv_widget_runtime_property_observer_writes_cache(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    handlers: dict[str, object] = {}

    class FakeObservingPlayer:
        core_shutdown = False
        pause = False

        def event_callback(self, *event_types):
            def register(callback):
                return callback

            return register

        def observe_property(self, name: str, handler) -> None:
            handlers[name] = handler

    widget._player = FakeObservingPlayer()
    widget._register_player_events()

    time_pos_handler = handlers.get("time-pos")
    assert callable(time_pos_handler)
    time_pos_handler("time-pos", 33.3)
    assert widget._observed_properties["time-pos"] == 33.3

    cache_state_handler = handlers.get("demuxer-cache-state")
    assert callable(cache_state_handler)
    cache_state_handler("demuxer-cache-state", {"cache-duration": 4.2})
    assert widget._observed_properties["demuxer-cache-state"] == {"cache-duration": 4.2}

    pause_handler = handlers.get("pause")
    assert callable(pause_handler)
    pause_handler("pause", True)
    assert widget._observed_properties["pause"] is True

    track_list_handler = handlers.get("track-list")
    assert callable(track_list_handler)
    track_list_handler("track-list", [{"id": 1, "type": "audio", "external": True, "selected": True}])
    assert widget._observed_properties["track-list"][0]["type"] == "audio"


def test_mpv_widget_create_player_passes_explicit_ytdlp_hook_path(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    recorded: dict[str, object] = {}

    class FakeMpvModule:
        @staticmethod
        def MPV(**kwargs):
            recorded.update(kwargs)
            return object()

    monkeypatch.setitem(sys.modules, "mpv", FakeMpvModule)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "atv_player.player.mpv_widget.resolve_mpv_ytdlp_path",
        lambda: "/tmp/tools/linux/yt-dlp",
    )

    widget._create_player()

    assert recorded["script_opts"] == "ytdl_hook-ytdl_path=/tmp/tools/linux/yt-dlp"


def test_mpv_widget_base_options_enable_user_mpv_config_dir_when_present(qtbot, monkeypatch, tmp_path) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setattr(mpv_widget_module, "resolve_mpv_config_dir", lambda: tmp_path)

    options = widget._base_player_options()

    assert options["config"] is True
    assert options["config_dir"] == str(tmp_path)


def test_mpv_widget_base_options_keep_default_without_user_mpv_config(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setattr(mpv_widget_module, "resolve_mpv_config_dir", lambda: None)

    options = widget._base_player_options()

    assert "config" not in options
    assert "config_dir" not in options


def test_mpv_widget_apply_shader_preset_switches_glsl_shaders(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakeAlivePlayer()
    widget._player = player
    monkeypatch.setattr(
        mpv_widget_module,
        "discover_shader_presets",
        lambda: [ShaderPreset(name="Anime4K-A", shader_files=("/tmp/a1.glsl", "/tmp/a2.glsl"))],
    )

    assert widget.apply_shader_preset("Anime4K-A") is True
    assert player.options["glsl-shaders"] == ["/tmp/a1.glsl", "/tmp/a2.glsl"]

    assert widget.apply_shader_preset("") is True
    assert player.options["glsl-shaders"] == []

    assert widget.apply_shader_preset("不存在") is False


def test_mpv_widget_applies_persisted_shader_preset_to_given_player(qtbot, monkeypatch) -> None:
    widget = MpvWidget(config=AppConfig(mpv_shader_preset="Anime4K-A"))
    qtbot.addWidget(widget)
    player = FakeAlivePlayer()
    monkeypatch.setattr(
        mpv_widget_module,
        "discover_shader_presets",
        lambda: [ShaderPreset(name="Anime4K-A", shader_files=("/tmp/a1.glsl",))],
    )

    widget._apply_shader_preset(player)

    assert player.options["glsl-shaders"] == ["/tmp/a1.glsl"]


def test_mpv_widget_logs_windows_runtime_diagnostics_around_player_creation(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    mpv_widget_module._WINDOWS_MPV_DIAGNOSTIC_STAGES_LOGGED.clear()

    recorded_stages: list[tuple[str, object | None, object | None, bool]] = []

    class FakeMpvModule:
        @staticmethod
        def MPV(**_kwargs):
            return object()

    monkeypatch.setitem(sys.modules, "mpv", FakeMpvModule)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("atv_player.player.mpv_widget.resolve_mpv_ytdlp_path", lambda: "")
    monkeypatch.setattr("atv_player.player.mpv_widget.resolve_mpv_ytdl_raw_options", lambda **_kwargs: "")
    monkeypatch.setattr(
        widget,
        "_log_windows_mpv_runtime_diagnostics",
        lambda stage, *, mpv_module=None, exc=None, force=False: recorded_stages.append(
            (stage, mpv_module, exc, force)
        ),
    )

    widget._create_player()

    assert [stage for stage, *_rest in recorded_stages] == [
        "before-import",
        "after-import",
        "before-create",
        "after-create",
    ]
    assert recorded_stages[1][1] is FakeMpvModule
    assert recorded_stages[2][1] is FakeMpvModule
    assert recorded_stages[3][1] is FakeMpvModule


def test_mpv_widget_logs_windows_runtime_diagnostics_when_player_creation_fails(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    mpv_widget_module._WINDOWS_MPV_DIAGNOSTIC_STAGES_LOGGED.clear()

    recorded_stages: list[tuple[str, object | None, object | None, bool]] = []

    class FakeMpvModule:
        @staticmethod
        def MPV(**_kwargs):
            raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "mpv", FakeMpvModule)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("atv_player.player.mpv_widget.resolve_mpv_ytdlp_path", lambda: "")
    monkeypatch.setattr("atv_player.player.mpv_widget.resolve_mpv_ytdl_raw_options", lambda **_kwargs: "")
    monkeypatch.setattr(
        widget,
        "_log_windows_mpv_runtime_diagnostics",
        lambda stage, *, mpv_module=None, exc=None, force=False: recorded_stages.append(
            (stage, mpv_module, exc, force)
        ),
    )

    with pytest.raises(RuntimeError, match="boom"):
        widget._create_player()

    assert [stage for stage, *_rest in recorded_stages] == [
        "before-import",
        "after-import",
        "before-create",
        "create-player-failed",
    ]
    assert isinstance(recorded_stages[-1][2], RuntimeError)
    assert recorded_stages[-1][3] is True


def test_mpv_widget_create_player_passes_ytdlp_raw_cookie_options(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    recorded: dict[str, object] = {}

    class FakeMpvModule:
        @staticmethod
        def MPV(**kwargs):
            recorded.update(kwargs)
            return object()

    monkeypatch.setitem(sys.modules, "mpv", FakeMpvModule)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "atv_player.player.mpv_widget.resolve_mpv_ytdlp_path",
        lambda: "/tmp/tools/linux/yt-dlp",
    )
    monkeypatch.setattr(
        "atv_player.player.mpv_widget.resolve_mpv_ytdl_raw_options",
        lambda **_kwargs: "cookies-from-browser=chrome",
    )

    widget._create_player()

    assert recorded["ytdl_raw_options"] == "cookies-from-browser=chrome"


def test_mpv_widget_uses_configured_base_playback_settings(qtbot, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    widget = MpvWidget(
        config=AppConfig(
            youtube_cookie_browser="edge",
            mpv_cache_size_mb=768,
            mpv_render_profile="software",
            mpv_network_timeout_seconds=22,
            mpv_default_readahead_secs=45,
        )
    )
    qtbot.addWidget(widget)
    monkeypatch.setitem(sys.modules, "mpv", types.SimpleNamespace(MPV=FakeMPV))
    monkeypatch.setattr(
        "atv_player.player.mpv_widget.resolve_mpv_ytdlp_path",
        lambda: "/tmp/tools/linux/yt-dlp",
    )

    widget._create_player()

    assert captured["hwdec"] == "no"
    # deinterlace 延后到实例存活后设置,避免旧 libmpv 拒绝 auto 导致构造失败
    assert "deinterlace" not in captured
    assert captured["demuxer_max_bytes"] == "768M"
    assert captured["network_timeout"] == 22
    assert captured["demuxer_readahead_secs"] == 45
    assert captured["ytdl_raw_options"] == "cookies-from-browser=edge,remote-components=ejs:github"


def test_mpv_widget_passes_auto_copy_hwdec_on_linux(qtbot, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    widget = MpvWidget(config=AppConfig(mpv_render_profile="balanced"))
    qtbot.addWidget(widget)
    monkeypatch.setitem(sys.modules, "mpv", types.SimpleNamespace(MPV=FakeMPV))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("atv_player.player.mpv_widget.detect_linux_nvidia_driver_mismatch", lambda: None)

    widget._create_player()

    assert captured["vo"] == "gpu"
    assert captured["hwdec"] == "auto-safe"
    assert captured["ao"] == "pulse,pipewire,alsa,"


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("compat", {"vo": "gpu", "hwdec": "auto-safe", "profile": "fast"}),
        ("vulkan", {"vo": "gpu-next", "gpu_api": "vulkan", "hwdec": "auto-safe"}),
        (
            "quality",
            {
                "vo": "gpu",
                "hwdec": "auto-safe",
                "scale": "ewa_lanczossharp",
                "cscale": "ewa_lanczossharp",
                "sigmoid_upscaling": "yes",
                "deband": "yes",
            },
        ),
        (
            "performance",
            {
                "vo": "gpu",
                "profile": "sw-fast",
                "vd_lavc_threads": 1,
                "deband": "no",
                "interpolation": "no",
            },
        ),
        ("copy-back", {"vo": "gpu", "hwdec": "auto-copy"}),
        ("software", {"vo": "gpu", "hwdec": "no"}),
    ],
)
def test_mpv_widget_applies_render_profile_options(qtbot, monkeypatch, profile, expected) -> None:
    captured: dict[str, object] = {}

    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    widget = MpvWidget(config=AppConfig(mpv_render_profile=profile))
    qtbot.addWidget(widget)
    monkeypatch.setitem(sys.modules, "mpv", types.SimpleNamespace(MPV=FakeMPV))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("atv_player.player.mpv_widget.detect_linux_nvidia_driver_mismatch", lambda: None)
    monkeypatch.delenv("ATV_GPU_VENDOR", raising=False)

    widget._create_player()

    for key, value in expected.items():
        assert captured[key] == value


@pytest.mark.parametrize(
    ("platform_name", "vendor", "expected_hwdec"),
    [
        ("linux", "nvidia", "nvdec"),
        ("win32", "nvidia", "nvdec"),
        ("win32", "intel", "d3d11va"),
        ("linux", "amd", "auto-safe"),
    ],
)
def test_mpv_widget_auto_render_profile_uses_platform_gpu_vendor(
    qtbot,
    monkeypatch,
    platform_name,
    vendor,
    expected_hwdec,
) -> None:
    captured: dict[str, object] = {}

    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    widget = MpvWidget(config=AppConfig(mpv_render_profile="auto"))
    qtbot.addWidget(widget)
    monkeypatch.setitem(sys.modules, "mpv", types.SimpleNamespace(MPV=FakeMPV))
    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.setenv("ATV_GPU_VENDOR", vendor)
    monkeypatch.setattr("atv_player.player.mpv_widget.detect_linux_nvidia_driver_mismatch", lambda: None)
    monkeypatch.setattr("atv_player.player.mpv_widget.resolve_mpv_ytdlp_path", lambda: "")
    monkeypatch.setattr("atv_player.player.mpv_widget.resolve_mpv_ytdl_raw_options", lambda **_kwargs: "")

    widget._create_player()

    assert captured["vo"] == "gpu"
    assert "gpu_api" not in captured
    assert captured["hwdec"] == expected_hwdec


def test_mpv_widget_falls_back_from_vulkan_creation_failure(qtbot, monkeypatch) -> None:
    attempts: list[dict[str, object]] = []

    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            attempts.append(dict(kwargs))
            if kwargs.get("gpu_api") == "vulkan":
                raise RuntimeError("vulkan unavailable")

    widget = MpvWidget(config=AppConfig(mpv_render_profile="vulkan"))
    qtbot.addWidget(widget)
    monkeypatch.setitem(sys.modules, "mpv", types.SimpleNamespace(MPV=FakeMPV))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("atv_player.player.mpv_widget.detect_linux_nvidia_driver_mismatch", lambda: None)

    player = widget._create_player()

    assert isinstance(player, FakeMPV)
    assert len(attempts) == 2
    assert attempts[0]["gpu_api"] == "vulkan"
    assert attempts[1]["vo"] == "gpu"
    assert "gpu_api" not in attempts[1]


def test_mpv_widget_fallback_reraises_original_creation_failure(qtbot, monkeypatch) -> None:
    attempts: list[dict[str, object]] = []

    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            attempts.append(dict(kwargs))
            raise RuntimeError("first failure" if len(attempts) == 1 else "fallback failure")

    widget = MpvWidget(config=AppConfig(mpv_render_profile="vulkan"))
    qtbot.addWidget(widget)
    monkeypatch.setitem(sys.modules, "mpv", types.SimpleNamespace(MPV=FakeMPV))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("atv_player.player.mpv_widget.detect_linux_nvidia_driver_mismatch", lambda: None)

    with pytest.raises(RuntimeError, match="first failure"):
        widget._create_player()

    assert len(attempts) == 3
    assert attempts[0]["gpu_api"] == "vulkan"
    assert attempts[1]["vo"] == "gpu"
    assert "gpu_api" not in attempts[1]
    assert attempts[2]["vo"] == "gpu"


def test_mpv_widget_refreshes_runtime_render_profile_on_existing_player(qtbot, monkeypatch) -> None:
    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.options: dict[str, object] = {}

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget = MpvWidget(config=AppConfig(mpv_render_profile="copy-back"))
    qtbot.addWidget(widget)
    fake_player = FakeMPV()
    widget._player = fake_player
    monkeypatch.setattr(sys, "platform", "linux")

    widget.apply_runtime_video_output_settings()

    assert fake_player.options["hwdec"] == "auto-copy"
    assert fake_player.options["deinterlace"] == "auto"


def test_mpv_widget_sets_deinterlace_auto_after_player_created(qtbot, monkeypatch) -> None:
    recorded: list[tuple[str, object]] = []

    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def __setitem__(self, key: str, value: object) -> None:
            recorded.append((key, value))

    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setitem(sys.modules, "mpv", types.SimpleNamespace(MPV=FakeMPV))

    widget._create_player()

    assert ("deinterlace", "auto") in recorded


def test_mpv_widget_falls_back_to_no_when_deinterlace_auto_rejected(qtbot, monkeypatch) -> None:
    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.deinterlace = None

        def __setitem__(self, key: str, value: object) -> None:
            if key == "deinterlace" and value == "auto":
                # mpv 0.27~0.38 只接受 yes/no,auto 会返回 OPTION_ERROR(-7)
                raise ValueError("Invalid value for mpv option", -7)
            if key == "deinterlace":
                self.deinterlace = value

    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setitem(sys.modules, "mpv", types.SimpleNamespace(MPV=FakeMPV))

    player = widget._create_player()
    assert player.deinterlace == "no"

    # 运行时刷新走同一条受保护的路径,且回退值进属性缓存
    widget._player = player
    widget._player_property_cache.clear()
    widget.apply_runtime_video_output_settings()
    assert player.deinterlace == "no"
    assert widget._player_property_cache["deinterlace"] == "no"


@pytest.mark.parametrize(
    ("hwdec", "expected"),
    [("auto-copy", True), ("auto-safe", False), ("no", False)],
)
def test_mpv_widget_reports_picture_adjustment_support_from_effective_hwdec(
    qtbot, hwdec, expected
) -> None:
    class FakeMPV:
        def __getitem__(self, key: str) -> object:
            assert key == "hwdec"
            return hwdec

    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = FakeMPV()

    assert widget.supports_picture_adjustments() is expected


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
            self._file_loaded_callback = None
            self._track_list_observer = None
            self._video_out_observer = None
            self._eof_reached_observer = None

        def event_callback(self, *event_types):
            def register(callback):
                if event_types == ("end-file",):
                    self._end_file_callback = callback
                else:
                    assert event_types == ("file-loaded",)
                    self._file_loaded_callback = callback
                return callback

            return register

        def observe_property(self, name: str, handler) -> None:
            if name == "track-list":
                self._track_list_observer = handler
                return
            if name == "video-out-params":
                self._video_out_observer = handler
                return
            if name == "chapter-list":
                self._chapter_list_observer = handler
                return
            if name == "eof-reached":
                self._eof_reached_observer = handler
                return
            if name in ("video-params", "video-format", "hwdec-current", "container-fps"):
                return
            if name in _RUNTIME_OBSERVED_PROPERTIES:
                return
            assert name == "pause"
            self._pause_observer = handler

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


def test_mpv_widget_emits_loading_and_visible_picture_states(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self._track_list_observer = None
            self._video_out_observer = None

        def event_callback(self, *event_types):
            def register(callback):
                return callback

            return register

        def observe_property(self, name: str, handler) -> None:
            if name == "track-list":
                self._track_list_observer = handler
            elif name == "video-out-params":
                self._video_out_observer = handler

        def play(self, url: str) -> None:
            return None

    widget._player = FakePlayer()
    widget._register_player_events()
    states: list[str] = []
    widget.video_picture_state_changed.connect(states.append)

    widget.load("http://m/1.m3u8")
    widget._player._video_out_observer("video-out-params", {"w": 1920, "h": 1080})

    assert states == ["loading", "visible"]


def test_mpv_widget_emits_unavailable_picture_state_when_track_list_has_no_video(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self._track_list_observer = None
            self._video_out_observer = None

        def event_callback(self, *event_types):
            def register(callback):
                return callback

            return register

        def observe_property(self, name: str, handler) -> None:
            if name == "track-list":
                self._track_list_observer = handler
            elif name == "video-out-params":
                self._video_out_observer = handler

        def play(self, url: str) -> None:
            return None

    widget._player = FakePlayer()
    widget._register_player_events()
    states: list[str] = []
    widget.video_picture_state_changed.connect(states.append)

    widget.load("http://m/1.m3u8")
    widget._player._track_list_observer("track-list", [{"id": 2, "type": "audio"}])

    assert states[-1] == "unavailable"


def test_mpv_widget_updates_volume_and_mute_state(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = FakeAlivePlayer()

    widget.set_volume(35)
    widget.toggle_mute()
    widget.toggle_mute()

    assert widget._player.volume == 35
    assert widget._player.mute is False


def test_mpv_widget_uses_direct_property_controls_on_windows(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setattr(sys, "platform", "win32")

    class FakePlayer:
        def __init__(self) -> None:
            self.core_shutdown = False
            self.command_calls: list[tuple[object, ...]] = []
            self.speed = 1.0
            self.volume = 100
            self.mute = False
            self.pause = False

        def command(self, *args) -> None:
            self.command_calls.append(args)

    widget._player = FakePlayer()

    widget.set_speed(1.5)
    widget.set_volume(35)
    widget.set_muted(True)
    widget.toggle_mute()
    widget.pause()
    widget.resume()

    assert widget._player.speed == 1.5
    assert widget._player.volume == 35
    assert widget._player.mute is False
    assert widget._player.pause is False
    assert widget._player.command_calls == []


def test_mpv_widget_toggles_video_info_overlay(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.command_calls: list[tuple[object, ...]] = []
            self.core_shutdown = False

        def command(self, *args) -> None:
            self.command_calls.append(args)

    widget._player = FakePlayer()

    widget.toggle_video_info()

    assert widget._player.command_calls == [
        ("script-binding", "stats/display-stats-toggle")
    ]


def test_mpv_widget_ignores_video_info_toggle_when_player_shuts_down(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.core_shutdown = False

        def command(self, *args) -> None:
            self.core_shutdown = True
            raise RuntimeError("core is gone")

    widget._player = FakePlayer()

    widget.toggle_video_info()


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
        ("http://m/1.m3u8", "replace", {"demuxer_lavf_o_add": "allowed_extensions=ALL"})
    ]
    assert widget._player.options == {
        "cache-pause": "yes",
        "cache-pause-initial": "yes",
        "cache-pause-wait": 3,
        "demuxer-readahead-secs": 20,
        "http-header-fields": [
            "User-Agent: Yamby/1.5.7.18(Android",
            "Referer: https://site.example",
        ]
    }


def test_mpv_widget_loads_m3u8_with_allowed_extensions_override(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.calls: list[tuple[str, str, object, dict[str, object]]] = []

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.calls.append((url, mode, index, options))

    widget._player = FakePlayer()

    widget.load("https://media.example/path/index.m3u8")

    assert widget._player.calls == [
        (
            "https://media.example/path/index.m3u8",
            "replace",
            None,
            {"demuxer_lavf_o_add": "allowed_extensions=ALL"},
        )
    ]


def test_mpv_widget_loads_mpd_with_allowed_extensions_override(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.calls: list[tuple[str, str, object, dict[str, object]]] = []

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.calls.append((url, mode, index, options))

    widget._player = FakePlayer()

    widget.load("http://127.0.0.1:2323/dash/test-token.mpd")

    assert widget._player.calls == [
        (
            "http://127.0.0.1:2323/dash/test-token.mpd",
            "replace",
            None,
            {"demuxer_lavf_o_add": "allowed_extensions=ALL"},
        )
    ]


def test_mpv_widget_uses_sync_loadfile_for_replace_to_preserve_stable_startup(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.loadfile_calls: list[tuple[str, str, object, dict[str, object]]] = []

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.loadfile_calls.append((url, mode, index, options))

    widget._player = FakePlayer()

    widget.load("http://127.0.0.1:2323/m3u", start_seconds=12)

    assert widget._player.loadfile_calls == [
        (
            "http://127.0.0.1:2323/m3u",
            "replace",
            None,
            {"start": "12"},
        )
    ]


def test_mpv_widget_uses_sync_loadfile_on_mpv_038_even_when_async_is_available(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.mpv_version_tuple = (0, 38, 0)
            self.loadfile_calls: list[tuple[str, str, object, dict[str, object]]] = []

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.loadfile_calls.append((url, mode, index, options))

        def command_async(self, *args, **kwargs):
            raise AssertionError("command_async should not be used in stable sync mode")

    widget._player = FakePlayer()

    widget.load("http://127.0.0.1:2323/dash/test-token.mpd", start_seconds=5)

    assert widget._player.loadfile_calls == [
        (
            "http://127.0.0.1:2323/dash/test-token.mpd",
            "replace",
            None,
            {
                "demuxer_lavf_o_add": "allowed_extensions=ALL",
                "start": "5",
            },
        )
    ]


def test_mpv_widget_uses_sync_loadfile_when_replacing_active_media_on_mpv_037(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.mpv_version_tuple = (0, 37, 0)
            self.path = "http://127.0.0.1:2323/current.m3u8"
            self.loadfile_calls: list[tuple[str, str, object, dict[str, object]]] = []
            self.command_async_calls: list[tuple[object, ...]] = []

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.loadfile_calls.append((url, mode, index, options))

        def command_async(self, *args, **kwargs):
            self.command_async_calls.append((*args, kwargs))
            return object()

    widget._player = FakePlayer()

    widget.load("http://127.0.0.1:2323/next.m3u8", start_seconds=12)

    assert widget._player.loadfile_calls == [
        (
            "http://127.0.0.1:2323/next.m3u8",
            "replace",
            None,
            {
                "demuxer_lavf_o_add": "allowed_extensions=ALL",
                "start": "12",
            },
        )
    ]
    assert widget._player.command_async_calls == []


def test_mpv_widget_uses_sync_loadfile_on_windows_even_when_async_is_available(
    qtbot, monkeypatch
) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.mpv_version_tuple = (0, 38, 0)
            self.loadfile_calls: list[tuple[str, str, object, dict[str, object]]] = []
            self.command_async_calls: list[tuple[object, ...]] = []

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.loadfile_calls.append((url, mode, index, options))

        def command_async(self, *args, **kwargs):
            self.command_async_calls.append((*args, kwargs))
            return object()

    monkeypatch.setattr("atv_player.player.mpv_widget.sys.platform", "win32")
    widget._player = FakePlayer()

    widget.load("https://media.example/video.mp4", start_seconds=2)

    assert widget._player.loadfile_calls == [
        (
            "https://media.example/video.mp4",
            "replace",
            None,
            {
                "start": "2",
            },
        )
    ]
    assert widget._player.command_async_calls == []


def test_mpv_widget_rejects_loadfile_option_values_with_commas(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    with pytest.raises(ValueError, match="cannot contain ','"):
        widget._encode_loadfile_options({"http-header-fields": "a=b,c=d"})


def test_mpv_widget_uses_hybrid_buffering_for_local_dash_proxy(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.calls: list[tuple[str, str, object, dict[str, object]]] = []
            self.options: dict[str, object] = {}

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.calls.append((url, mode, index, options))

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    widget.load("http://127.0.0.1:2323/dash/test-token.mpd")

    assert widget._player.options["cache-pause"] == "yes"
    assert widget._player.options["cache-pause-initial"] == "yes"
    assert widget._player.options["cache-pause-wait"] == 5
    assert widget._player.options["demuxer-readahead-secs"] == 120
    assert "cache-secs" not in widget._player.options


def test_mpv_widget_loads_external_audio_file_with_video(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.calls: list[tuple[str, str, object, dict[str, object]]] = []
            self.audio_add_calls: list[str] = []
            self.options: dict[str, object] = {}

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.calls.append((url, mode, index, options))

        def audio_add(self, url: str) -> None:
            self.audio_add_calls.append(url)

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    widget.load(
        "https://media.example/video-1080.mp4",
        audio_files="https://media.example/audio.webm",
    )

    assert widget._player.calls == [
        (
            "https://media.example/video-1080.mp4",
            "replace",
            None,
            {},
        )
    ]
    assert widget._player.audio_add_calls == ["https://media.example/audio.webm"]
    assert widget._player.options["cache-pause"] == "no"
    assert widget._player.options["cache-pause-initial"] == "no"
    assert widget._player.options["cache-pause-wait"] == 0
    assert widget._player.options["demuxer-readahead-secs"] == 3


def test_mpv_widget_adds_external_audio_via_command_when_url_contains_commas(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.calls: list[tuple[str, str, object, dict[str, object]]] = []
            self.audio_add_calls: list[str] = []

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.calls.append((url, mode, index, options))

        def audio_add(self, url: str) -> None:
            self.audio_add_calls.append(url)

    widget._player = FakePlayer()

    widget.load(
        "https://media.example/video-1080.mp4",
        audio_files="https://media.example/audio.webm?lsparams=a,b,c",
    )

    assert widget._player.calls == [
        (
            "https://media.example/video-1080.mp4",
            "replace",
            None,
            {},
        )
    ]
    assert widget._player.audio_add_calls == ["https://media.example/audio.webm?lsparams=a,b,c"]


def test_mpv_widget_loads_youtube_page_url_with_ytdl_format(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.calls: list[tuple[str, str, object, dict[str, object]]] = []
            self.options: dict[str, object] = {}

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.calls.append((url, mode, index, options))

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    widget.load(
        "https://www.youtube.com/watch?v=test123",
        ytdl_format="299+140",
    )

    assert widget._player.calls == [
        (
            "https://www.youtube.com/watch?v=test123",
            "replace",
            None,
            {"ytdl": "yes", "ytdl_format": "299+140"},
        )
    ]
    assert widget._player.options["cache-pause"] == "yes"
    assert widget._player.options["cache-pause-initial"] == "yes"
    assert widget._player.options["cache-pause-wait"] == 5
    assert widget._player.options["demuxer-readahead-secs"] == 120
    assert "cache-secs" not in widget._player.options


def test_mpv_widget_loads_mkv_with_subtitle_preroll_disabled(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.calls: list[tuple[str, str, object, dict[str, object]]] = []

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.calls.append((url, mode, index, options))

    widget._player = FakePlayer()

    widget.load("http://media.example/video.mkv")

    assert widget._player.calls == [
        (
            "http://media.example/video.mkv",
            "replace",
            None,
            {"demuxer_mkv_subtitle_preroll_secs": "0"},
        )
    ]


def test_mpv_widget_loads_local_iso_proxy_as_mpegts_with_linearized_timestamps(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.calls: list[tuple[str, str, object, dict[str, object]]] = []

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.calls.append((url, mode, index, options))

    widget._player = FakePlayer()

    widget.load("http://127.0.0.1:2323/iso/test-token/BDMV/PLAYLIST/00002.MPLS")

    assert widget._player.calls == [
        (
            "http://127.0.0.1:2323/iso/test-token/BDMV/PLAYLIST/00002.MPLS",
            "replace",
            None,
            {
                "demuxer_lavf_format": "mpegts",
                "demuxer_lavf_linearize_timestamps": "yes",
                "rebase_start_time": "yes",
            },
        )
    ]


def test_mpv_widget_disables_initial_cache_pause_for_local_iso_proxy_and_restores_defaults(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.calls: list[tuple[str, str, object, dict[str, object]]] = []
            self.options: dict[str, object] = {}

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.calls.append((url, mode, index, options))

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    widget.load("http://127.0.0.1:2323/iso/test-token/BDMV/PLAYLIST/00002.MPLS")

    assert widget._player.options["cache-pause"] == "no"
    assert widget._player.options["cache-pause-initial"] == "no"
    assert widget._player.options["cache-pause-wait"] == 0
    assert widget._player.options["demuxer-readahead-secs"] == 3

    widget.load("http://m/1.m3u8")

    assert widget._player.options["cache-pause"] == "yes"
    assert widget._player.options["cache-pause-initial"] == "yes"
    assert widget._player.options["cache-pause-wait"] == 3
    assert widget._player.options["demuxer-readahead-secs"] == 20


def test_mpv_widget_keeps_special_readahead_profiles_for_ytdlp_sources(qtbot) -> None:
    widget = MpvWidget(config=AppConfig(mpv_default_readahead_secs=33))
    qtbot.addWidget(widget)
    player = FakeAlivePlayer()
    widget._player = player

    profile_name = widget._apply_stream_profile(
        player,
        "https://www.youtube.com/watch?v=test123",
        ytdl_format="bestvideo+bestaudio/best",
    )

    assert profile_name == "hybrid-ytdl"
    assert player.options["demuxer-readahead-secs"] == 120


def test_mpv_widget_uses_dash_direct_profile_for_asset_with_external_audio(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakeAlivePlayer()
    widget._player = player

    profile_name = widget._apply_stream_profile(
        player,
        "http://127.0.0.1:2323/dash/asset/test-token/0.m4s",
        audio_files="http://127.0.0.1:2323/dash/asset/test-token/1.m4s",
    )

    assert profile_name == "dash-direct-external-audio"
    assert player.options["cache-pause"] == "no"
    assert player.options["cache-pause-initial"] == "no"
    assert player.options["demuxer-readahead-secs"] == 30


def test_mpv_widget_keeps_dash_proxy_profile_for_manifest_without_external_audio(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakeAlivePlayer()
    widget._player = player

    profile_name = widget._apply_stream_profile(
        player,
        "http://127.0.0.1:2323/dash/test-token.mpd",
    )

    assert profile_name == "dash-proxy"
    assert player.options["demuxer-readahead-secs"] == 120


def test_mpv_widget_extra_options_override_profile_values(qtbot) -> None:
    widget = MpvWidget(
        config=AppConfig(mpv_extra_options="demuxer-readahead-secs=9\ncache-pause-wait=1")
    )
    qtbot.addWidget(widget)
    player = FakeAlivePlayer()
    widget._player = player

    widget._apply_stream_profile(
        player,
        "https://www.youtube.com/watch?v=test123",
        ytdl_format="best",
    )
    widget._apply_extra_mpv_options(player)

    assert player.options["demuxer-readahead-secs"] == "9"
    assert player.options["cache-pause-wait"] == "1"


def test_mpv_widget_load_uses_cover_art_file_when_poster_path_is_provided(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.loadfile_calls: list[tuple[str, dict[str, object]]] = []
            self.command_calls: list[tuple[object, ...]] = []
            self.options: dict[str, object] = {}

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.loadfile_calls.append((url, options))

        def command(self, *args) -> None:
            self.command_calls.append(args)

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()
    states: list[str] = []
    widget.video_picture_state_changed.connect(states.append)

    widget.load("http://m/1.mp3", start_seconds=5, poster_image_path="/tmp/cover.jpg")

    assert widget._player.loadfile_calls == [
        (
            "http://m/1.mp3",
            {
                "start": "5",
                "cover_art_files": "/tmp/cover.jpg",
                "audio_display": "external-first",
            },
        )
    ]
    assert widget._player.command_calls == []
    assert widget._player.options == {
        "cache-pause": "yes",
        "cache-pause-initial": "yes",
        "cache-pause-wait": 3,
        "demuxer-readahead-secs": 20,
        "http-header-fields": [],
    }
    assert states == ["loading"]


def test_mpv_widget_attach_audio_cover_adds_albumart_video_track(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.command_calls: list[tuple[object, ...]] = []
            self.options: dict[str, object] = {}

        def command(self, *args) -> None:
            self.command_calls.append(args)

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()
    states: list[str] = []
    widget.video_picture_state_changed.connect(states.append)

    widget.attach_audio_cover("/tmp/cover.jpg")

    assert widget._player.command_calls == [
        ("video-add", "/tmp/cover.jpg", "select", "", "", True)
    ]
    assert widget._player.options == {
        "audio-display": "external-first",
        "image-display-duration": "inf",
        "keep-open": "yes",
    }
    assert states == ["audio-cover"]


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

    assert widget._player.loadfile_calls == ["http://m/1.m3u8", "http://m/2.m3u8"]
    assert widget._player.play_calls == []
    assert widget._player.options == {
        "cache-pause": "yes",
        "cache-pause-initial": "yes",
        "cache-pause-wait": 3,
        "demuxer-readahead-secs": 20,
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


def test_mpv_widget_stop_media_stops_current_file_without_terminating_player(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        core_shutdown = False

        def __init__(self) -> None:
            self.commands: list[str] = []
            self.terminated = False

        def command(self, name: str) -> None:
            self.commands.append(name)

        def terminate(self) -> None:
            self.terminated = True

    player = FakePlayer()
    widget._player = player

    widget.stop_media()

    assert player.commands == ["stop"]
    assert player.terminated is False
    assert widget._player is player


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
    assert captured["hwdec"] == "auto-safe"
    assert captured["force_window"] == "yes"
    assert captured["cache"] is True
    assert captured["cache_pause_initial"] is True
    assert captured["cache_pause_wait"] == 3
    assert "cache_secs" not in captured
    assert "deinterlace" not in captured
    assert captured["demuxer_max_bytes"] == "512M"
    assert captured["demuxer_max_back_bytes"] == "128M"
    assert captured["stream_buffer_size"] == "4M"
    assert captured["network_timeout"] == 15
    assert "log_handler" not in captured
    assert "loglevel" not in captured


def test_mpv_widget_enables_mpv_terminal_logging_only_when_debug_is_requested(qtbot, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setitem(sys.modules, "mpv", types.SimpleNamespace(MPV=FakeMPV))
    monkeypatch.setenv("ATV_MPV_DEBUG", "1")

    widget._create_player()

    assert captured["log_handler"] is print
    assert captured["loglevel"] == "debug"


def test_mpv_widget_uses_auto_windows_renderer_defaults(qtbot, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    widget = MpvWidget(config=AppConfig())
    qtbot.addWidget(widget)
    monkeypatch.setitem(sys.modules, "mpv", types.SimpleNamespace(MPV=FakeMPV))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ATV_GPU_VENDOR", "intel")
    monkeypatch.setattr("atv_player.player.mpv_widget.resolve_mpv_ytdlp_path", lambda: "")
    monkeypatch.setattr("atv_player.player.mpv_widget.resolve_mpv_ytdl_raw_options", lambda **_kwargs: "")

    widget._create_player()

    assert captured["vo"] == "gpu"
    assert "gpu_api" not in captured
    assert captured["hwdec"] == "d3d11va"
    assert "start_event_thread" not in captured


def test_mpv_widget_uses_linux_audio_output_fallbacks_without_forcing_device_name(qtbot, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setitem(sys.modules, "mpv", types.SimpleNamespace(MPV=FakeMPV))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("atv_player.player.mpv_widget.detect_linux_nvidia_driver_mismatch", lambda: None)

    widget._create_player()

    assert captured["vo"] == "gpu"
    assert captured["ao"] == "pulse,pipewire,alsa,"
    assert "audio_device" not in captured


def test_mpv_widget_forces_software_video_output_when_nvidia_driver_versions_mismatch(qtbot, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeMPV:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setitem(sys.modules, "mpv", types.SimpleNamespace(MPV=FakeMPV))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "atv_player.player.mpv_widget.detect_linux_nvidia_driver_mismatch",
        lambda: ("580.159.03", "580.142"),
    )

    widget._create_player()

    assert captured["hwdec"] == "no"
    assert captured["vo"] == "x11"
    assert captured["ao"] == "pulse,pipewire,alsa,"


def test_mpv_widget_emits_playback_finished_only_for_natural_end(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._end_file_callback = None
            self._file_loaded_callback = None

        def event_callback(self, *event_types):
            def register(callback):
                if event_types == ("end-file",):
                    self._end_file_callback = callback
                else:
                    assert event_types == ("file-loaded",)
                    self._file_loaded_callback = callback
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


def test_mpv_widget_emits_playback_finished_when_audio_cover_reaches_eof(qtbot) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.loadfile_calls: list[tuple[str, dict[str, object]]] = []
            self.options: dict[str, object] = {}
            self._end_file_callback = None
            self._file_loaded_callback = None
            self._track_list_observer = None
            self._video_out_observer = None
            self._eof_reached_observer = None

        def event_callback(self, *event_types):
            def register(callback):
                if event_types == ("end-file",):
                    self._end_file_callback = callback
                else:
                    assert event_types == ("file-loaded",)
                    self._file_loaded_callback = callback
                return callback

            return register

        def observe_property(self, name: str, handler) -> None:
            if name == "track-list":
                self._track_list_observer = handler
                return
            if name == "video-out-params":
                self._video_out_observer = handler
                return
            if name == "chapter-list":
                self._chapter_list_observer = handler
                return
            if name == "eof-reached":
                self._eof_reached_observer = handler
                return
            if name in ("video-params", "video-format", "hwdec-current", "container-fps"):
                return
            if name in _RUNTIME_OBSERVED_PROPERTIES:
                return
            assert name == "pause"
            self._pause_observer = handler

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.loadfile_calls.append((url, options))

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakePlayer()
    widget._player = player
    widget._register_player_events()
    finished = {"count": 0}
    widget.playback_finished.connect(lambda: finished.__setitem__("count", finished["count"] + 1))

    widget.load("http://m/1.mp3", poster_image_path="/tmp/cover.jpg")
    player._eof_reached_observer("eof-reached", True)
    player._end_file_callback(types.SimpleNamespace(data=types.SimpleNamespace(reason=0)))

    assert player.loadfile_calls == [
        (
            "http://m/1.mp3",
            {
                "cover_art_files": "/tmp/cover.jpg",
                "audio_display": "external-first",
            },
        )
    ]
    assert finished["count"] == 1


def test_mpv_widget_emits_playback_failed_with_reason_from_end_file_event(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._end_file_callback = None
            self._file_loaded_callback = None

        def event_callback(self, *event_types):
            def register(callback):
                if event_types == ("end-file",):
                    self._end_file_callback = callback
                else:
                    assert event_types == ("file-loaded",)
                    self._file_loaded_callback = callback
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
            self._file_loaded_callback = None

        def event_callback(self, *event_types):
            def register(callback):
                if event_types == ("end-file",):
                    self._end_file_callback = callback
                else:
                    assert event_types == ("file-loaded",)
                    self._file_loaded_callback = callback
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
            self._file_loaded_callback = None

        def event_callback(self, *event_types):
            def register(callback):
                if event_types == ("end-file",):
                    self._end_file_callback = callback
                else:
                    assert event_types == ("file-loaded",)
                    self._file_loaded_callback = callback
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


def test_mpv_widget_formats_numeric_mpv_error_codes_from_end_file_event(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._end_file_callback = None
            self._file_loaded_callback = None

        def event_callback(self, *event_types):
            def register(callback):
                if event_types == ("end-file",):
                    self._end_file_callback = callback
                else:
                    assert event_types == ("file-loaded",)
                    self._file_loaded_callback = callback
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
    player._end_file_callback(types.SimpleNamespace(data=types.SimpleNamespace(reason=4, error=-20)))

    assert failures == ["播放失败: 未指定错误 (-20)"]


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
    clicked = {"count": 0}
    widget.left_clicked.connect(lambda: clicked.__setitem__("count", clicked["count"] + 1))

    widget.load("http://m/1.m3u8")

    assert player.play_calls == ["http://m/1.m3u8"]
    assert player._left_click_handler is not None

    player._left_click_handler("d", "MBTN_LEFT", None, None, None)

    assert clicked["count"] == 1
    assert dismissed["count"] == 1


def test_mpv_widget_registers_mpv_mouse_bindings_on_windows(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self.register_key_binding_calls: list[tuple[str, str]] = []

        def event_callback(self, *_event_types):
            def register(callback):
                return callback

            return register

        def observe_property(self, _name: str, _handler) -> None:
            return None

        def register_key_binding(self, keydef: str, callback, mode: str = "force") -> None:
            del callback
            self.register_key_binding_calls.append((keydef, mode))

        def play(self, url: str) -> None:
            self.play_calls.append(url)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakePlayer()
    monkeypatch.setattr("atv_player.player.mpv_widget.sys.platform", "win32")
    monkeypatch.setattr(widget, "_create_player", lambda: player)

    widget.load("http://m/1.m3u8")

    assert player.register_key_binding_calls == [
        ("MBTN_RIGHT", "force"),
        ("MBTN_LEFT", "force"),
    ]


def test_mpv_widget_registers_property_observers_on_windows(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self._end_file_callback = None
            self._file_loaded_callback = None
            self.observed_properties: list[str] = []

        def event_callback(self, *event_types):
            def register(callback):
                if event_types == ("end-file",):
                    self._end_file_callback = callback
                else:
                    assert event_types == ("file-loaded",)
                    self._file_loaded_callback = callback
                return callback

            return register

        def observe_property(self, name: str, _handler) -> None:
            self.observed_properties.append(name)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakePlayer()
    widget._player = player
    monkeypatch.setattr("atv_player.player.mpv_widget.sys.platform", "win32")

    widget._register_player_events()

    assert callable(player._end_file_callback)
    assert callable(player._file_loaded_callback)
    assert player.observed_properties == [
        "track-list",
        "chapter-list",
        "video-out-params",
        "eof-reached",
        "pause",
        "video-params",
        "video-format",
        "hwdec-current",
        "container-fps",
        *_RUNTIME_OBSERVED_PROPERTIES,
    ]


def test_mpv_widget_routes_windows_property_sets_to_gui_thread(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setattr("atv_player.player.mpv_widget.sys.platform", "win32")

    main_thread_id = threading.get_ident()
    background_thread_id: int | None = None
    assigned_values: list[tuple[float, int]] = []

    class FakePlayer:
        def __init__(self) -> None:
            self.core_shutdown = False
            self._speed = 1.0

        @property
        def speed(self) -> float:
            return self._speed

        @speed.setter
        def speed(self, value: float) -> None:
            self._speed = value
            assigned_values.append((value, threading.get_ident()))

    widget._player = FakePlayer()

    def run() -> None:
        nonlocal background_thread_id
        background_thread_id = threading.get_ident()
        widget.set_speed(1.5)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join()
    qtbot.waitUntil(lambda: len(assigned_values) == 1, timeout=1000)

    assert background_thread_id is not None
    assert assigned_values == [(1.5, main_thread_id)]
    assert assigned_values[0][1] != background_thread_id


def test_mpv_widget_applies_repeated_property_sets_on_windows_via_direct_assignment(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setattr("atv_player.player.mpv_widget.sys.platform", "win32")

    class FakePlayer:
        def __init__(self) -> None:
            self.core_shutdown = False
            self.speed_history: list[float] = []
            self.mute_history: list[bool] = []
            self._speed = 1.0
            self._mute = False

        @property
        def speed(self) -> float:
            return self._speed

        @speed.setter
        def speed(self, value: float) -> None:
            self._speed = value
            self.speed_history.append(value)

        @property
        def mute(self) -> bool:
            return self._mute

        @mute.setter
        def mute(self, value: bool) -> None:
            self._mute = value
            self.mute_history.append(value)

    player = FakePlayer()
    widget._player = player

    widget.set_speed(1.25)
    widget.set_speed(1.25)
    widget.set_muted(True)
    widget.set_muted(True)

    assert player.speed_history == [1.25, 1.25]
    assert player.mute_history == [True, True]


def test_mpv_widget_duration_reads_observed_property_cache(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setattr("atv_player.player.mpv_widget.sys.platform", "win32")

    class FakePlayer:
        pass

    widget._player = FakePlayer()
    widget._observed_properties["duration"] = 3672.8

    assert widget.duration_seconds() == 3672


def test_mpv_widget_applies_startup_properties_on_windows_like_other_platforms(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setattr("atv_player.player.mpv_widget.sys.platform", "win32")

    class FakePlayer:
        def __init__(self) -> None:
            self.pause = False
            self.loadfile_calls: list[tuple[str, str, object, dict[str, object]]] = []
            self.options: dict[str, object] = {}

        def loadfile(self, url: str, mode: str = "replace", index=None, **options) -> None:
            self.loadfile_calls.append((url, mode, index, options))

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    widget.load(
        "http://m/1.m3u8",
        pause=True,
        headers={
            "User-Agent": "Yamby/1.5.7.18(Android",
            "Referer": "https://site.example",
        },
    )

    assert widget._player.loadfile_calls == [
        (
            "http://m/1.m3u8",
            "replace",
            None,
            {"demuxer_lavf_o_add": "allowed_extensions=ALL"},
        )
    ]
    assert widget._player.options["http-header-fields"] == [
        "User-Agent: Yamby/1.5.7.18(Android",
        "Referer: https://site.example",
    ]
    assert widget._player.options["cache-pause-initial"] == "yes"


def test_mpv_widget_emits_subtitle_tracks_changed_when_mpv_track_list_updates(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._track_list_observer = None
            self._video_out_observer = None
            self._eof_reached_observer = None

        def event_callback(self, *event_types):
            def register(callback):
                return callback

            return register

        def observe_property(self, name: str, handler) -> None:
            if name == "track-list":
                self._track_list_observer = handler
                return
            if name == "video-out-params":
                self._video_out_observer = handler
                return
            if name == "chapter-list":
                self._chapter_list_observer = handler
                return
            if name == "eof-reached":
                self._eof_reached_observer = handler
                return
            if name in ("video-params", "video-format", "hwdec-current", "container-fps"):
                return
            if name in _RUNTIME_OBSERVED_PROPERTIES:
                return
            assert name == "pause"
            self._pause_observer = handler

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


def test_mpv_widget_translates_common_subtitle_titles_to_chinese(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = types.SimpleNamespace(
        track_list=[
            {"id": 1, "type": "sub", "lang": "", "title": "Simplified", "default": False, "forced": False, "external": False},
            {"id": 2, "type": "sub", "lang": "", "title": "Traditional Chinese", "default": False, "forced": False, "external": False},
            {"id": 3, "type": "sub", "lang": "", "title": "cht", "default": True, "forced": False, "external": False},
            {"id": 4, "type": "sub", "lang": "eng", "title": "English", "default": False, "forced": False, "external": False},
            {"id": 5, "type": "sub", "lang": "eng", "title": "Director Commentary", "default": False, "forced": False, "external": False},
        ]
    )

    assert [track.label for track in widget.subtitle_tracks()] == [
        "简体中文",
        "繁体中文",
        "繁体中文 (默认)",
        "English",
        "Director Commentary",
    ]


def test_mpv_widget_translates_bilingual_subtitle_titles(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = types.SimpleNamespace(
        track_list=[
            {"id": 1, "type": "sub", "lang": "chi", "title": "chs&eng", "default": False, "forced": False, "external": False},
            {"id": 2, "type": "sub", "lang": "chi", "title": "cht&eng", "default": False, "forced": False, "external": False},
            {"id": 3, "type": "sub", "lang": "", "title": "中英", "default": False, "forced": False, "external": False},
        ]
    )

    assert [track.label for track in widget.subtitle_tracks()] == ["简英双语", "繁英双语", "中英双语"]


def test_mpv_widget_auto_mode_prefers_bilingual_subtitle_even_over_default_simplified(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        sid="auto",
        track_list=[
            {"id": 2, "type": "sub", "lang": "chi", "title": "Simplified", "default": True, "forced": False, "external": False},
            {"id": 6, "type": "sub", "lang": "chi", "title": "chs&eng", "default": False, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_subtitle_mode("auto")

    assert applied_track_id == 6
    assert player.sid == 6


def test_mpv_widget_disambiguates_subtitle_tracks_with_identical_labels(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = types.SimpleNamespace(
        track_list=[
            {"id": 1, "type": "sub", "lang": "zh-hans", "title": "", "codec": "ass", "default": False, "forced": False, "external": False},
            {"id": 2, "type": "sub", "lang": "zh-hans", "title": "", "codec": "subrip", "default": False, "forced": False, "external": False},
            {"id": 3, "type": "sub", "lang": "zh-hans", "title": "", "codec": "hdmv_pgs_subtitle", "default": True, "forced": False, "external": False},
        ]
    )

    assert [track.label for track in widget.subtitle_tracks()] == [
        "简体中文 [ASS]",
        "简体中文 [SRT]",
        "简体中文 (默认)",
    ]


def test_mpv_widget_auto_mode_selects_simplified_for_chi_titled_subrip_tracks(qtbot) -> None:
    # Reproduces a real MKV whose only subtitle metadata is lang=chi + a
    # "Simplified"/"Traditional" title (subrip). Auto mode must pick Simplified.
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        sid="auto",
        track_list=[
            {"id": 2, "type": "sub", "lang": "chi", "title": "Simplified", "default": False, "forced": False, "external": False},
            {"id": 3, "type": "sub", "lang": "chi", "title": "Traditional", "default": False, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_subtitle_mode("auto")

    assert applied_track_id == 2
    assert player.sid == 2


def test_mpv_widget_auto_mode_selects_simplified_when_lang_is_missing(qtbot) -> None:
    # mpv sometimes exposes subrip tracks with only a title and no lang; the
    # title tokens alone must still drive Simplified selection.
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        sid="auto",
        track_list=[
            {"id": 2, "type": "sub", "lang": "", "title": "Traditional", "default": True, "forced": False, "external": False},
            {"id": 3, "type": "sub", "lang": "", "title": "Simplified", "default": False, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_subtitle_mode("auto")

    assert applied_track_id == 3
    assert player.sid == 3


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


def test_mpv_widget_auto_mode_recognizes_bare_simplified_and_traditional_titles(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        sid="auto",
        track_list=[
            {"id": 5, "type": "sub", "lang": "", "title": "Traditional", "default": True, "forced": False, "external": False},
            {"id": 6, "type": "sub", "lang": "", "title": "Simplified", "default": False, "forced": False, "external": False},
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


def test_mpv_widget_reports_when_subtitle_track_is_present(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = types.SimpleNamespace(
        track_list=[
            {"id": 9, "type": "audio"},
            {"id": 11, "type": "sub", "external": True},
        ]
    )

    assert widget.has_subtitle_track(11) is True
    assert widget.has_subtitle_track(12) is False


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


def test_mpv_widget_can_load_and_remove_external_secondary_subtitle(qtbot, tmp_path) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    subtitle_path = tmp_path / "danmaku.srt"
    subtitle_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")

    class FakePlayer:
        def __init__(self) -> None:
            self.command_calls: list[tuple[object, ...]] = []
            self.track_list: list[dict[str, object]] = []
            self.secondary_sid: object = "no"

        def command(self, *args) -> None:
            self.command_calls.append(args)
            if args == ("sub-add", str(subtitle_path), "auto"):
                self.track_list.append({"id": 99, "type": "sub", "external": True})
            elif args == ("sub-remove", 99):
                self.track_list = [track for track in self.track_list if track.get("id") != 99]

    player = FakePlayer()
    widget._player = player

    track_id = widget.load_external_subtitle(str(subtitle_path), select_for_secondary=True)
    widget.remove_subtitle_track(track_id)

    assert track_id == 99
    assert player.secondary_sid == "no"
    assert player.command_calls == [
        ("sub-add", str(subtitle_path), "auto"),
        ("sub-remove", 99),
    ]


def test_mpv_widget_waits_for_external_subtitle_track_to_appear_after_sub_add(qtbot, tmp_path) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    subtitle_path = tmp_path / "lyrics.ass"
    subtitle_path.write_text("dummy", encoding="utf-8")

    class FakePlayer:
        def __init__(self) -> None:
            self.command_calls: list[tuple[object, ...]] = []
            self.secondary_sid: object = "no"
            self._subtitle_added = False
            self._post_add_track_reads = 0

        @property
        def track_list(self) -> list[dict[str, object]]:
            if not self._subtitle_added:
                return []
            self._post_add_track_reads += 1
            if self._post_add_track_reads == 1:
                return []
            return [{"id": 99, "type": "sub", "external": True}]

        def command(self, *args) -> None:
            self.command_calls.append(args)
            if args == ("sub-add", str(subtitle_path), "auto"):
                self._subtitle_added = True

    player = FakePlayer()
    widget._player = player

    track_id = widget.load_external_subtitle(str(subtitle_path), select_for_secondary=True)

    assert track_id == 99
    assert player.secondary_sid == 99
    assert player.command_calls == [("sub-add", str(subtitle_path), "auto")]


def test_mpv_widget_ignores_removing_stale_external_subtitle_track(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.command_calls: list[tuple[object, ...]] = []
            self.track_list: list[dict[str, object]] = []
            self.secondary_sid: object = 99

        def command(self, *args) -> None:
            self.command_calls.append(args)
            raise RuntimeError(
                (
                    "Error running mpv command",
                    -12,
                    (object(), object(), object()),
                )
            )

    player = FakePlayer()
    widget._player = player

    widget.remove_subtitle_track(99)

    assert player.secondary_sid == "no"
    assert player.command_calls == []


def test_mpv_widget_still_raises_when_removing_existing_subtitle_track_fails(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.command_calls: list[tuple[object, ...]] = []
            self.track_list: list[dict[str, object]] = [{"id": 99, "type": "sub", "external": True}]
            self.secondary_sid: object = "no"

        def command(self, *args) -> None:
            self.command_calls.append(args)
            raise RuntimeError(
                (
                    "Error running mpv command",
                    -12,
                    (object(), object(), object()),
                )
            )

    player = FakePlayer()
    widget._player = player

    with pytest.raises(RuntimeError, match="Error running mpv command"):
        widget.remove_subtitle_track(99)

    assert player.command_calls == [("sub-remove", 99)]


def test_mpv_widget_uses_track_queries_and_external_subtitles_on_windows_like_other_platforms(
    qtbot, monkeypatch, tmp_path
) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setattr("atv_player.player.mpv_widget.sys.platform", "win32")

    class FakePlayer:
        def __init__(self) -> None:
            self.track_list = [
                {"id": 1, "type": "sub", "title": "CN"},
                {"id": 2, "type": "audio", "title": "Main"},
            ]
            self.command_calls: list[tuple[object, ...]] = []
            self._next_track_id = 3

        def command(self, *args) -> None:
            self.command_calls.append(args)
            if args[:2] == ("sub-add", str(subtitle_path)):
                self.track_list.append({"id": self._next_track_id, "type": "sub", "title": "外挂", "external": True})
                self._next_track_id += 1

    widget._player = FakePlayer()
    subtitle_path = tmp_path / "sample.ass"
    subtitle_path.write_text("dummy", encoding="utf-8")

    assert [track.id for track in widget.subtitle_tracks()] == [1]
    assert [track.id for track in widget.audio_tracks()] == [2]
    assert widget.load_external_subtitle(str(subtitle_path), select_for_secondary=False) == 3
    widget.remove_subtitle_track(1)
    assert widget._player.command_calls == [("sub-add", str(subtitle_path), "auto"), ("sub-remove", 1)]


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


def test_mpv_widget_reads_and_writes_subtitle_delay(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"sub-delay": 0.0}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.subtitle_delay() == 0.0

    widget.set_subtitle_delay(1.5)

    assert widget.subtitle_delay() == 1.5


def test_mpv_widget_reads_and_writes_audio_delay(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"audio-delay": 0.0}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.audio_delay() == 0.0

    widget.set_audio_delay(-0.5)

    assert widget.audio_delay() == -0.5


def test_mpv_widget_delay_setters_clamp_to_range(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    widget.set_subtitle_delay(999.0)
    assert widget.subtitle_delay() == 10.0
    widget.set_subtitle_delay(-999.0)
    assert widget.subtitle_delay() == -10.0
    widget.set_audio_delay(999.0)
    assert widget.audio_delay() == 10.0
    widget.set_audio_delay(-999.0)
    assert widget.audio_delay() == -10.0


def test_mpv_widget_reads_and_writes_picture_adjustment(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    for prop in ("brightness", "contrast", "saturation", "hue", "gamma"):
        getter = getattr(widget, prop)
        setter = getattr(widget, "set_" + prop)
        assert getter() == 0
        setter(25)
        assert getter() == 25


def test_mpv_widget_picture_adjustment_clamps_to_range(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    for prop in ("brightness", "contrast", "saturation", "hue", "gamma"):
        setter = getattr(widget, "set_" + prop)
        getter = getattr(widget, prop)
        setter(999)
        assert getter() == 100
        setter(-999)
        assert getter() == -100


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


def test_mpv_widget_reads_and_writes_primary_subtitle_ass_override(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"sub-ass-override": "scale"}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.subtitle_ass_override() == "scale"

    widget.set_subtitle_ass_override("no")

    assert widget.subtitle_ass_override() == "no"


def test_mpv_widget_normalizes_boolean_primary_subtitle_ass_override_values(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"sub-ass-override": True}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.subtitle_ass_override() == "yes"

    widget.set_subtitle_ass_override(widget.subtitle_ass_override())

    assert widget.subtitle_ass_override() == "yes"


def test_mpv_widget_reads_and_writes_secondary_subtitle_ass_override(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"secondary-sub-ass-override": "strip"}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.secondary_subtitle_ass_override() == "strip"

    widget.set_secondary_subtitle_ass_override("no")

    assert widget.secondary_subtitle_ass_override() == "no"


def test_mpv_widget_normalizes_boolean_secondary_subtitle_ass_override_values(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"secondary-sub-ass-override": False}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.secondary_subtitle_ass_override() == "no"

    widget.set_secondary_subtitle_ass_override(widget.secondary_subtitle_ass_override())

    assert widget.secondary_subtitle_ass_override() == "no"


def test_mpv_widget_reports_primary_subtitle_ass_override_unsupported_when_property_is_missing(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __getitem__(self, key: str) -> object:
            raise RuntimeError(("mpv property does not exist", -8, (object(), b"options/sub-ass-override", b"scale")))

    widget._player = FakePlayer()

    assert widget.supports_subtitle_ass_override() is False


def test_mpv_widget_reports_secondary_subtitle_ass_override_unsupported_when_property_is_missing(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __getitem__(self, key: str) -> object:
            raise RuntimeError(
                ("mpv property does not exist", -8, (object(), b"options/secondary-sub-ass-override", b"strip"))
            )

    widget._player = FakePlayer()

    assert widget.supports_secondary_subtitle_ass_override() is False


def test_mpv_widget_emits_audio_tracks_changed_when_mpv_track_list_updates(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._track_list_observer = None
            self._video_out_observer = None
            self._eof_reached_observer = None

        def event_callback(self, *event_types):
            def register(callback):
                return callback

            return register

        def observe_property(self, name: str, handler) -> None:
            if name == "track-list":
                self._track_list_observer = handler
                return
            if name == "video-out-params":
                self._video_out_observer = handler
                return
            if name == "chapter-list":
                self._chapter_list_observer = handler
                return
            if name == "eof-reached":
                self._eof_reached_observer = handler
                return
            if name in ("video-params", "video-format", "hwdec-current", "container-fps"):
                return
            if name in _RUNTIME_OBSERVED_PROPERTIES:
                return
            assert name == "pause"
            self._pause_observer = handler

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


def test_mpv_widget_audio_track_labels_include_distinguishing_metadata(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = types.SimpleNamespace(
        track_list=[
            {
                "id": 1,
                "type": "audio",
                "lang": "cmn",
                "title": "",
                "default": True,
                "forced": False,
                "external": False,
                "codec": "aac",
                "audio-channels": 2,
                "audio-samplerate": 48000,
            },
            {
                "id": 2,
                "type": "audio",
                "lang": "cmn",
                "title": "",
                "default": True,
                "forced": False,
                "external": False,
                "codec": "ac3",
                "audio-channels": 6,
                "audio-samplerate": 48000,
            },
        ]
    )

    assert [track.label for track in widget.audio_tracks()] == [
        "国语 (默认) [AAC / 2ch / 48000Hz / ID 1]",
        "国语 (默认) [AC3 / 6ch / 48000Hz / ID 2]",
    ]


def test_mpv_widget_auto_mode_keeps_mpv_auto_selection(qtbot) -> None:
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

    assert applied_track_id is None
    assert player.aid == "auto"


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


def test_mpv_widget_demuxer_cache_duration_reads_observed_property_cache(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setattr("atv_player.player.mpv_widget.sys.platform", "win32")

    class FakePlayer:
        pass

    widget._player = FakePlayer()
    widget._observed_properties["demuxer-cache-duration"] = 42.7

    assert widget.demuxer_cache_duration_seconds() == 42


def test_mpv_widget_demuxer_cache_duration_returns_zero_when_missing(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    monkeypatch.setattr("atv_player.player.mpv_widget.sys.platform", "win32")

    class FakePlayer:
        pass

    widget._player = FakePlayer()

    assert widget.demuxer_cache_duration_seconds() == 0


def test_mpv_widget_demuxer_cache_duration_returns_zero_without_player(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    widget._player = None

    assert widget.demuxer_cache_duration_seconds() == 0


def test_mpv_widget_lists_chapters_sorted_with_readable_labels(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = types.SimpleNamespace(
        chapter_list=[
            {"title": "片尾", "time": 1380.5},
            {"title": "", "time": 0.0},
            {"title": "正片", "time": 92.0},
        ]
    )

    assert widget.chapters() == [
        Chapter(index=0, title="", start_seconds=0.0, label="章节 1"),
        Chapter(index=1, title="正片", start_seconds=92.0, label="正片"),
        Chapter(index=2, title="片尾", start_seconds=1380.5, label="片尾"),
    ]


def test_mpv_widget_chapters_skips_entries_with_unusable_time(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = types.SimpleNamespace(
        chapter_list=[
            {"title": "A", "time": 0.0},
            {"title": "缺失时间"},
            {"title": "非数字", "time": "abc"},
            {"title": "布尔", "time": True},
            {"title": "无穷", "time": float("inf")},
            {"title": "非字典"},
            "not-a-dict",
            {"title": "B", "time": 30.0},
        ]
    )

    assert [chapter.label for chapter in widget.chapters()] == ["A", "B"]


def test_mpv_widget_chapters_returns_empty_without_player_or_property(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    widget._player = None
    assert widget.chapters() == []

    widget._player = types.SimpleNamespace()
    assert widget.chapters() == []

    widget._player = types.SimpleNamespace(chapter_list=None)
    assert widget.chapters() == []


def test_mpv_widget_emits_chapters_changed_on_chapter_list_observation(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self._chapter_list_observer = None

        def event_callback(self, *_names):
            def decorator(func):
                return func

            return decorator

        def observe_property(self, name: str, handler) -> None:
            if name == "chapter-list":
                self._chapter_list_observer = handler

        def play(self, url: str) -> None:
            self.play_calls.append(url)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakePlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: player)
    changes = {"count": 0}
    widget.chapters_changed.connect(lambda: changes.__setitem__("count", changes["count"] + 1))

    widget.load("http://m/1.mkv")
    assert player._chapter_list_observer is not None
    player._chapter_list_observer("chapter-list", [{"title": "正片", "time": 92.0}])

    assert changes["count"] == 1


def test_mpv_widget_reports_current_video_height_from_video_out_params(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    assert widget.current_video_height() is None

    widget._video_out_params = {"pixelformat": "yuv420p", "w": 1920, "h": 1080}
    assert widget.current_video_height() == 1080

    widget._video_out_params = {"w": 1920, "h": 0}
    assert widget.current_video_height() is None


def test_mpv_widget_shutdown_terminates_on_background_thread(
    qtbot, monkeypatch
) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    terminate_threads: list[str] = []

    class OkPlayer:
        core_shutdown = False

        def terminate(self) -> None:
            terminate_threads.append(threading.current_thread().name)

    widget._player = OkPlayer()
    monkeypatch.setattr(mpv_widget_module, "_MPV_TERMINATE_TIMEOUT_SECONDS", 5.0)

    widget.shutdown()

    assert widget._player is None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not terminate_threads:
        time.sleep(0.01)
    assert terminate_threads == ["mpv-terminate"]


def test_mpv_widget_shutdown_returns_promptly_when_terminate_hangs(
    qtbot, monkeypatch, caplog
) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    release = threading.Event()

    class WedgedPlayer:
        core_shutdown = False

        def terminate(self) -> None:
            release.wait(30)

    widget._player = WedgedPlayer()
    monkeypatch.setattr(mpv_widget_module, "_MPV_TERMINATE_TIMEOUT_SECONDS", 0.05)

    try:
        with caplog.at_level("ERROR", logger="atv_player.player.mpv_widget"):
            started = time.monotonic()
            widget.shutdown()
            elapsed = time.monotonic() - started

            assert elapsed < 5
            assert widget._player is None
            leak_logged = "leaking the player instance" in caplog.text
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not leak_logged:
                time.sleep(0.01)
                leak_logged = "leaking the player instance" in caplog.text
        assert "leaking the player instance" in caplog.text
    finally:
        release.set()


class _StarvationFakePlayer:
    core_shutdown = False

    def __init__(self) -> None:
        self.audio_reload_calls = 0

    def audio_reload(self) -> None:
        self.audio_reload_calls += 1


def _seed_starvation_cache(widget: MpvWidget) -> None:
    """模拟 observe_property 已推送的看门狗采样值。"""
    widget._observed_properties.update(
        {
            "pause": False,
            "playback-time": 100.0,
            "audio-pts": 50.0,
            "track-list": [
                {"id": 1, "type": "video"},
                {"id": 2, "type": "audio", "external": True, "selected": True},
            ],
        }
    )


_STARVATION_AUDIO_URL = "http://127.0.0.1:2323/dash/asset/tok/1.m4s"
_MPV_WIDGET_MAX_STARVATION_ROUNDS = mpv_widget_module._AUDIO_STARVATION_MAX_RELOADS


def _tick_starvation(widget, player, clock, *, playback_delta=2.0, audio_pts_delta=0.0) -> None:
    del player  # 采样值直接从 observe 缓存读,不再经过 player
    cache = widget._observed_properties
    if "playback-time" in cache:
        cache["playback-time"] = float(cache["playback-time"]) + playback_delta
    if "audio-pts" in cache:
        cache["audio-pts"] = float(cache["audio-pts"]) + audio_pts_delta
    widget._check_external_audio_starvation()
    clock["now"] += 2.0


def test_mpv_widget_audio_starvation_watchdog_fires_when_audio_pts_freezes(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = _StarvationFakePlayer()
    widget._player = player
    widget._external_audio_files = _STARVATION_AUDIO_URL
    widget._audio_pts_supported = True
    _seed_starvation_cache(widget)

    clock = {"now": 1000.0}
    monkeypatch.setattr(mpv_widget_module.time, "monotonic", lambda: clock["now"])

    starved: list[str] = []
    widget.external_audio_starved.connect(starved.append)

    for _ in range(4):
        _tick_starvation(widget, player, clock)
    assert starved == []  # 样本数不足

    _tick_starvation(widget, player, clock)
    assert starved == [_STARVATION_AUDIO_URL]
    assert widget._audio_starvation_reloads == 1
    # 触发后样本清空,不会连发
    _tick_starvation(widget, player, clock)
    assert starved == [_STARVATION_AUDIO_URL]


def test_mpv_widget_audio_starvation_watchdog_stays_silent_while_audio_advances(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = _StarvationFakePlayer()
    widget._player = player
    widget._external_audio_files = _STARVATION_AUDIO_URL
    widget._audio_pts_supported = True
    _seed_starvation_cache(widget)

    clock = {"now": 1000.0}
    monkeypatch.setattr(mpv_widget_module.time, "monotonic", lambda: clock["now"])

    starved: list[str] = []
    widget.external_audio_starved.connect(starved.append)

    for _ in range(8):
        _tick_starvation(widget, player, clock, audio_pts_delta=2.0)
    assert starved == []
    assert widget._audio_starvation_reloads == 0


def test_mpv_widget_audio_starvation_watchdog_ignores_pause_and_missing_track(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = _StarvationFakePlayer()
    widget._player = player
    widget._external_audio_files = _STARVATION_AUDIO_URL
    widget._audio_pts_supported = True
    _seed_starvation_cache(widget)

    clock = {"now": 1000.0}
    monkeypatch.setattr(mpv_widget_module.time, "monotonic", lambda: clock["now"])

    starved: list[str] = []
    widget.external_audio_starved.connect(starved.append)

    widget._observed_properties["pause"] = True
    for _ in range(6):
        _tick_starvation(widget, player, clock)
    assert starved == []

    widget._observed_properties["pause"] = False
    widget._observed_properties["track-list"] = [{"id": 1, "type": "video"}]
    for _ in range(6):
        _tick_starvation(widget, player, clock)
    assert starved == []


def test_mpv_widget_audio_starvation_watchdog_respects_cooldown_and_gives_up(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = _StarvationFakePlayer()
    widget._player = player
    widget._external_audio_files = _STARVATION_AUDIO_URL
    widget._audio_pts_supported = True
    _seed_starvation_cache(widget)

    clock = {"now": 1000.0}
    monkeypatch.setattr(mpv_widget_module.time, "monotonic", lambda: clock["now"])

    starved: list[str] = []
    attach_failed: list[str] = []
    widget.external_audio_starved.connect(starved.append)
    widget.external_audio_attach_failed.connect(attach_failed.append)

    for round_index in range(_MPV_WIDGET_MAX_STARVATION_ROUNDS):
        for _ in range(5):
            _tick_starvation(widget, player, clock)
        if round_index == 0:
            assert starved == [_STARVATION_AUDIO_URL]
        # 冷却期内的持续断粮不触发
        assert len(starved) == round_index + 1
        clock["now"] += 31.0

    # 达到重挂上限:停止看门狗并显性提示无声音
    for _ in range(5):
        _tick_starvation(widget, player, clock)
    assert len(starved) == _MPV_WIDGET_MAX_STARVATION_ROUNDS
    assert attach_failed == [_STARVATION_AUDIO_URL]
    assert widget._audio_starvation_timer.isActive() is False


def test_mpv_widget_reload_external_audio_uses_audio_reload_command(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = _StarvationFakePlayer()
    widget._player = player

    assert widget.reload_external_audio() is True
    assert player.audio_reload_calls == 1


def test_mpv_widget_audio_starvation_watchdog_disables_without_audio_pts_support(qtbot) -> None:
    class LegacyPlayer(_StarvationFakePlayer):
        @property
        def audio_pts(self):
            raise AttributeError(
                "('mpv property does not exist', -8, (<MpvHandle object at 0x1>, b'audio-pts'))"
            )

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = LegacyPlayer()
    widget._player = player
    widget._external_audio_files = _STARVATION_AUDIO_URL

    starved: list[str] = []
    widget.external_audio_starved.connect(starved.append)

    widget._check_external_audio_starvation()
    # 首轮即探测到属性缺失:禁用看门狗,不采样也不崩溃
    assert widget._audio_pts_supported is False
    assert widget._audio_starvation_timer.isActive() is False

    for _ in range(6):
        _tick_starvation(widget, player, {"now": time.monotonic()})
    assert starved == []
