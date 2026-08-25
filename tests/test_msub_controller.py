# ruff: noqa: E501
"""服务端追剧播放控制器(MsubController)单测:播放列表构建/懒解析/进度约定。"""
from atv_player.controllers.msub_controller import MsubController, parse_msub_vod_id
from atv_player.models import PlayItem


class FakeApiClient:
    base_url = "http://192.168.50.60:4567"
    username = "harold"

    def __init__(self, detail=None, play_result=None, play_error=None):
        self.detail = detail or {}
        self.play_result = play_result or {}
        self.play_error = play_error
        self.resolve_calls: list[tuple[int, int]] = []

    def get_media_subscription_detail(self, subscription_id):
        return self.detail

    def resolve_msub_episode(self, subscription_id, episode):
        self.resolve_calls.append((subscription_id, episode))
        if self.play_error is not None:
            raise self.play_error
        return self.play_result


def _detail_payload():
    return {
        "subscription": {
            "id": 5,
            "name": "凡人修仙传",
            "season": 1,
            "cover": "http://x/cover.jpg",
            "officialEpisodes": 4,
            "officialTotal": 12,
            "currentEpisodes": 3,
        },
        "media": {"name": "凡人修仙传", "overview": "少年修仙", "year": "2020", "cover": "http://x/cover.jpg"},
        "episodes": [
            {"episode": 1, "title": "启程", "present": True},
            {"episode": 2, "title": "入门", "present": True},
            {"episode": 3, "title": None, "present": True},
            {"episode": 4, "title": "未播", "present": False},
        ],
    }


def test_parse_msub_vod_id() -> None:
    assert parse_msub_vod_id("msub:12") == 12
    assert parse_msub_vod_id("12") == 12
    assert parse_msub_vod_id("msubep-12-3") == 0  # msubep 是集级链接,不是订阅标识
    assert parse_msub_vod_id("garbage") == 0


def test_build_request_playlist_contains_present_episodes_only() -> None:
    api = FakeApiClient(detail=_detail_payload())
    request = MsubController(api).build_request("msub:5")

    assert request.source_kind == "msub"
    assert request.vod.vod_id == "msub:5"
    assert request.vod.vod_name == "凡人修仙传"
    assert request.source_key == "http://192.168.50.60:4567"
    assert [item.title for item in request.playlist] == ["第1集 启程", "第2集 入门", "第3集"]
    assert all(not item.url for item in request.playlist)
    # 逻辑集 id:进度回传约定(vodId=msub:{id} + episodeUrl 含 msubep-{id}-{ep})
    assert [item.play_id for item in request.playlist] == ["msubep-5-1", "msubep-5-2", "msubep-5-3"]
    assert request.async_playback_loader is True


def test_build_request_start_episode_picks_next_unwatched() -> None:
    api = FakeApiClient(detail=_detail_payload())
    request = MsubController(api).build_request("msub:5", start_episode=3)
    assert request.clicked_index == 2

    request = MsubController(api).build_request("msub:5", start_episode=99)
    assert request.clicked_index == 2  # 超出时落到最后一集


def test_build_request_without_playable_episodes_raises() -> None:
    payload = _detail_payload()
    payload["episodes"] = [{"episode": 1, "present": False}]
    api = FakeApiClient(detail=payload)
    try:
        MsubController(api).build_request("msub:5")
    except ValueError as exc:
        assert "暂无可播剧集" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_load_playback_item_resolves_url_headers_and_subtitles() -> None:
    api = FakeApiClient(
        play_result={
            "url": "http://d/01.mkv",
            "type": "alias",
            "header": {"User-Agent": "pan/1.0", "Referer": "http://d/"},
            "subt": "http://d/01.srt",
            "subs": [{"name": "中文字幕", "lang": "chi", "url": "http://d/01.ass"}],
        }
    )
    controller = MsubController(api)
    item = PlayItem(title="第1集 启程", url="", play_id="msubep-5-1")
    controller.load_playback_item(item)

    assert api.resolve_calls == [(5, 1)]
    assert item.url == "http://d/01.mkv"
    assert item.headers == {"User-Agent": "pan/1.0", "Referer": "http://d/"}
    assert item.original_url == "msubep-5-1"  # 网盘直链带时效,history 只留逻辑 id
    assert [subtitle.url for subtitle in item.external_subtitles] == ["http://d/01.srt", "http://d/01.ass"]


def test_load_playback_item_raises_without_url() -> None:
    api = FakeApiClient(play_result={"url": ""})
    item = PlayItem(title="第2集", url="", play_id="msubep-5-2")
    try:
        MsubController(api).load_playback_item(item)
    except ValueError as exc:
        assert "没有可用播放地址" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_load_playback_item_without_msubep_id_raises() -> None:
    api = FakeApiClient(play_result={"url": "http://x"})
    item = PlayItem(title="无标识", url="", play_id="")
    try:
        MsubController(api).load_playback_item(item)
    except ValueError as exc:
        assert "缺少服务端追剧集标识" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_history_hooks_wired_to_repository_identity() -> None:
    saved: list[tuple[str, dict]] = []
    api = FakeApiClient(detail=_detail_payload())
    controller = MsubController(
        api,
        playback_history_loader=lambda vod_id: None,
        playback_history_saver=lambda vod_id, payload: saved.append((vod_id, payload)),
    )
    request = controller.build_request("msub:5")
    assert request.playback_history_loader is not None
    assert request.playback_history_loader() is None
    request.playback_history_saver({"position": 1000})
    assert saved == [("msub:5", {"position": 1000})]
