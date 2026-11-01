"""下集预加载:调度守卫、解析落盘、/driver 预注册消费与失效。"""

import base64
import json
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from atv_player.controllers.player_controller import PlayerSession
from atv_player.models import AppConfig, PlaybackLoadResult, PlayItem, VodItem
from atv_player.ui import player_window as player_window_module
from atv_player.ui.player_window import PlayerWindow, _NextEpisodePreload


@pytest.fixture(autouse=True)
def prevent_real_mpv_load(monkeypatch: pytest.MonkeyPatch) -> None:
    # 与 test_player_window_ui.py 相同的护栏:避免测试里真的创建 libmpv 核心。
    monkeypatch.setattr(
        "atv_player.player.mpv_widget.MpvWidget.load",
        lambda self, *args, **kwargs: None,
    )


@pytest.fixture(autouse=True)
def isolate_player_window_cache_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        player_window_module, "app_cache_dir", lambda: tmp_path / "app-cache"
    )


class FakePlayerController:
    def report_progress(self, *args, **kwargs) -> None:
        return None

    def resolve_play_item_detail(self, session, play_item):
        return None

    def stop_playback(self, session, current_index: int) -> None:
        return None

    def on_item_started(self, session, current_index: int) -> None:
        return None


class StubVideo:
    def __init__(self, position: int = 30, duration: int = 120) -> None:
        self.position = position
        self.duration = duration

    def position_seconds(self) -> int:
        return self.position

    def duration_seconds(self) -> int:
        return self.duration


def make_session(item_count: int = 2, start_index: int = 0) -> PlayerSession:
    return PlayerSession(
        vod=VodItem(vod_id="series-1", vod_name="Series"),
        playlist=[
            PlayItem(title=f"Episode {index + 1}", url=f"http://m/{index + 1}.mp4")
            for index in range(item_count)
        ],
        start_index=start_index,
        start_position_seconds=0,
        speed=1.0,
    )


def make_window(qtbot, config: AppConfig | None = None) -> PlayerWindow:
    window = PlayerWindow(FakePlayerController(), config=config)
    qtbot.addWidget(window)
    return window


def test_preload_resolves_next_item_and_records_freshness(qtbot) -> None:
    window = make_window(qtbot)
    session = make_session()
    session.playlist[1].url = ""
    loader_calls: list[PlayItem] = []

    def loader(item: PlayItem):
        loader_calls.append(item)
        item.url = f"http://m/resolved{len(loader_calls)}.mp4"
        return None

    session.playback_loader = loader
    window.open_session(session)
    assert window.current_index == 0

    window._preload_next_episode(window.session, 1)

    # open_session 会先同步解析当前集,预载再解析下一集。
    assert loader_calls == [session.playlist[0], session.playlist[1]]
    assert session.playlist[1].url == "http://m/resolved2.mp4"
    preload = window._next_episode_preload
    assert preload is not None
    assert preload.item is session.playlist[1]
    assert preload.prepared_url == ""


def test_preload_prepares_cenc_proxy_url(qtbot, monkeypatch) -> None:
    # CENC 密文流(红果 #hg-cenc= 片段):预载时即预建本地解密会话(m3u8_ad_filter
    # 内部 create_cenc_media_url+warmup 预热索引),本地地址进 prepared_url,
    # 切集经现成消费管线直接复用,索引构建不再落在切集时刻。
    window = make_window(qtbot)
    session = make_session()
    # 片段 payload 须为合法 base64url JSON(与运行时 build_cenc_media_fragment 同构)
    payload = base64.urlsafe_b64encode(
        json.dumps({"spade_a": "KEY"}).encode("utf-8")
    ).decode("ascii")
    fragment_url = f"http://m/2.mp4#hg-cenc={payload}"
    session.playlist[1].url = fragment_url
    session.playback_loader = None  # url 已设,loader 早退
    window.open_session(session)

    local_url = "http://127.0.0.1:4567/cenc/tok.mp4"
    prepare_calls: list[str] = []

    def fake_prepare(url, headers=None, **kwargs):
        prepare_calls.append(url)
        return local_url if "#hg-cenc=" in url else ""

    monkeypatch.setattr(window._m3u8_ad_filter, "prepare", fake_prepare)

    window._preload_next_episode(window.session, 1)

    preload = window._next_episode_preload
    assert preload is not None
    assert preload.item is session.playlist[1]
    assert preload.prepared_url == local_url
    assert preload.source_url == fragment_url
    assert prepare_calls == [fragment_url]
    # 切集消费:source_url 匹配直接复用本地地址
    assert window._take_next_episode_preloaded_url(
        session.playlist[1], fragment_url
    ) == local_url


def test_preload_cenc_prepare_failure_falls_back_silently(qtbot, monkeypatch) -> None:
    # 预建失败静默:prepared_url 留空,切集走原 prepare 流程兜底
    window = make_window(qtbot)
    session = make_session()
    payload = base64.urlsafe_b64encode(
        json.dumps({"spade_a": "KEY"}).encode("utf-8")
    ).decode("ascii")
    session.playlist[1].url = f"http://m/2.mp4#hg-cenc={payload}"
    session.playback_loader = None
    window.open_session(session)

    def boom(url, headers=None, **kwargs):
        raise RuntimeError("proxy down")

    monkeypatch.setattr(window._m3u8_ad_filter, "prepare", boom)

    window._preload_next_episode(window.session, 1)

    preload = window._next_episode_preload
    assert preload is not None
    assert preload.prepared_url == ""
    assert preload.source_url == ""


def test_preload_skips_replacement_loader_results(qtbot) -> None:
    window = make_window(qtbot)
    session = make_session()
    session.playlist[1].url = ""

    def loader(item: PlayItem):
        item.url = "http://m/2.mp4"
        return PlaybackLoadResult(
            replacement_playlist=[PlayItem(title="替换", url="http://m/2.mp4")],
            replacement_start_index=0,
        )

    session.playback_loader = loader
    window.open_session(session)

    window._preload_next_episode(window.session, 1)

    assert window._next_episode_preload is None


def test_take_preloaded_url_consumes_matches_and_expires(qtbot) -> None:
    window = make_window(qtbot)
    session = make_session()
    window.open_session(session)
    item = session.playlist[0]
    window._next_episode_preload = _NextEpisodePreload(
        item=item,
        index=0,
        prepared_url="http://127.0.0.1:2323/driver/abc123",
        source_url="http://m/1.mp4",
        created_at=time.monotonic(),
    )

    assert window._take_next_episode_preloaded_url(item, "http://m/1.mp4") == (
        "http://127.0.0.1:2323/driver/abc123"
    )
    assert window._next_episode_preload is None
    # 已消费:再次取或换 item/source 均返回空串。
    assert window._take_next_episode_preloaded_url(item, "http://m/1.mp4") == ""

    window._next_episode_preload = _NextEpisodePreload(
        item=item,
        index=0,
        prepared_url="http://127.0.0.1:2323/driver/abc123",
        source_url="http://m/1.mp4",
        created_at=0.0,
    )
    assert window._take_next_episode_preloaded_url(item, "http://m/1.mp4") == ""

    window._next_episode_preload = _NextEpisodePreload(
        item=item,
        index=0,
        prepared_url="http://127.0.0.1:2323/driver/abc123",
        source_url="http://m/other.mp4",
        created_at=time.monotonic(),
    )
    assert window._take_next_episode_preloaded_url(item, "http://m/1.mp4") == ""


def test_playback_prepare_consumes_preloaded_drive_url(qtbot, monkeypatch) -> None:
    window = make_window(qtbot)
    proxy_url = "http://127.0.0.1:4567/p/site/12@proxy"
    driver_url = "http://127.0.0.1:2323/driver/abc123"
    session = make_session()
    session.playlist[0].url = proxy_url

    # open_session 自身会为当前集入队一次 prepare(会真的做网络探测),先屏蔽掉,
    # 只验证本次显式调用对预载结果的消费。
    real_start_prepare = window._start_playback_prepare
    monkeypatch.setattr(window, "_start_playback_prepare", lambda **_kwargs: False)
    window.open_session(session)
    monkeypatch.setattr(window, "_start_playback_prepare", real_start_prepare)
    window.video = StubVideo()

    drive_calls: list[tuple[PlayItem, str]] = []

    def fail_drive_prepare(item: PlayItem, source_url: str) -> str:
        drive_calls.append((item, source_url))
        return ""

    monkeypatch.setattr(window, "_prepare_drive_parallel_url", fail_drive_prepare)
    started: list[dict] = []
    monkeypatch.setattr(
        window,
        "_start_current_item_playback",
        lambda **kwargs: started.append(kwargs),
    )
    window._next_episode_preload = _NextEpisodePreload(
        item=session.playlist[0],
        index=0,
        prepared_url=driver_url,
        source_url=proxy_url,
        created_at=time.monotonic(),
    )

    assert window._start_playback_prepare(
        previous_index=0,
        start_position_seconds=0,
        pause=False,
    )

    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline and not started:
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        time.sleep(0.01)

    assert drive_calls == []
    assert session.playlist[0].url == driver_url
    assert session.playlist[0].original_url == proxy_url
    assert started


def test_schedule_guards_disabled_live_and_last_index(qtbot, monkeypatch) -> None:
    calls: list[tuple[PlayerSession, int]] = []
    called = threading.Event()

    def record_preload(self, session: PlayerSession, next_index: int) -> None:
        calls.append((session, next_index))
        called.set()

    monkeypatch.setattr(PlayerWindow, "_run_next_episode_preload", record_preload)

    config = AppConfig(next_episode_preload_enabled=False)
    window = make_window(qtbot, config=config)
    session = make_session()
    window.open_session(session)
    window._maybe_schedule_next_episode_preload()
    assert calls == []

    window.config = AppConfig()
    session.source_kind = "live"
    window._maybe_schedule_next_episode_preload()
    assert calls == []

    session.source_kind = ""
    window.current_index = len(session.playlist) - 1
    window._maybe_schedule_next_episode_preload()
    assert calls == []

    window.current_index = 0
    window._maybe_schedule_next_episode_preload()
    assert called.wait(2.0)
    assert calls == [(session, 1)]


def test_schedule_skips_while_fresh_stash_exists(qtbot, monkeypatch) -> None:
    calls: list[tuple[PlayerSession, int]] = []

    def record_preload(self, session: PlayerSession, next_index: int) -> None:
        calls.append((session, next_index))

    monkeypatch.setattr(PlayerWindow, "_run_next_episode_preload", record_preload)
    window = make_window(qtbot)
    session = make_session()
    window.open_session(session)
    window._next_episode_preload = _NextEpisodePreload(
        item=session.playlist[1],
        index=1,
        prepared_url="",
        created_at=time.monotonic(),
    )

    window._maybe_schedule_next_episode_preload()

    assert calls == []


def test_report_progress_near_end_schedules_preload(qtbot, monkeypatch) -> None:
    calls: list[tuple[PlayerSession, int]] = []
    called = threading.Event()

    def record_preload(self, session: PlayerSession, next_index: int) -> None:
        calls.append((session, next_index))
        called.set()

    monkeypatch.setattr(PlayerWindow, "_run_next_episode_preload", record_preload)
    window = make_window(qtbot)
    session = make_session()
    window.open_session(session)
    window.video = StubVideo(position=1700, duration=1800)

    window.report_progress()

    assert called.wait(2.0)
    assert calls == [(session, 1)]


def test_report_progress_far_from_end_does_not_schedule(qtbot, monkeypatch) -> None:
    calls: list[tuple[PlayerSession, int]] = []

    def record_preload(self, session: PlayerSession, next_index: int) -> None:
        calls.append((session, next_index))

    monkeypatch.setattr(PlayerWindow, "_run_next_episode_preload", record_preload)
    window = make_window(qtbot)
    session = make_session()
    window.open_session(session)
    window.video = StubVideo(position=30, duration=3600)

    window.report_progress()

    assert calls == []


def test_open_session_clears_stale_preload(qtbot) -> None:
    window = make_window(qtbot)
    first = make_session()
    window.open_session(first)
    window._next_episode_preload = _NextEpisodePreload(
        item=first.playlist[1],
        index=1,
        prepared_url="http://127.0.0.1:2323/driver/old",
        source_url="http://old/1",
        created_at=time.monotonic(),
    )

    window.open_session(make_session())

    assert window._next_episode_preload is None
