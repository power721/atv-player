from pathlib import Path

import pytest

import atv_player.danmaku.cache as danmaku_cache_module
import atv_player.danmaku.generic as generic_danmaku_module
from atv_player.danmaku.generic import GenericDanmakuController
from atv_player.danmaku.models import (
    DanmakuSeriesPreference,
    DanmakuSourceGroup,
    DanmakuSourceOption,
    DanmakuSourceSearchResult,
)
from atv_player.danmaku.preferences import DanmakuSeriesPreferenceStore
from atv_player.models import PlayItem


@pytest.mark.parametrize(
    "value",
    ["", "v.qq.com/x/1", "ftp://v.qq.com/x/1", "https:///x/1"],
)
def test_normalize_danmaku_episode_url_rejects_incomplete_urls(value: str) -> None:
    with pytest.raises(ValueError, match=r"完整的 http\(s\) 单集链接"):
        generic_danmaku_module.normalize_danmaku_episode_url(value)


def test_normalize_danmaku_episode_url_strips_surrounding_whitespace() -> None:
    assert (
        generic_danmaku_module.normalize_danmaku_episode_url(
            "  https://v.qq.com/x/cover/demo/ep1.html  "
        )
        == "https://v.qq.com/x/cover/demo/ep1.html"
    )


def test_generic_controller_downloads_episode_url_without_replacing_candidates(
    monkeypatch,
) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.resolve_calls: list[str] = []

        def provider_key_for_url(self, page_url: str) -> str:
            assert page_url == "https://v.qq.com/x/cover/demo/ep1.html"
            return "tencent"

        def resolve_danmu(self, page_url: str) -> str:
            self.resolve_calls.append(page_url)
            return '<i><d p="1,1,25,16777215">manual</d></i>'

    saved: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        generic_danmaku_module,
        "load_cached_danmaku_xml",
        lambda _name, _url: "",
    )
    monkeypatch.setattr(
        generic_danmaku_module,
        "save_cached_danmaku_xml",
        lambda name, url, xml: saved.append((name, url, xml)),
    )
    candidates = [
        DanmakuSourceGroup(
            provider="youku",
            provider_label="优酷",
            options=[
                DanmakuSourceOption(
                    provider="youku",
                    name="旧候选",
                    url="https://youku/old",
                )
            ],
        )
    ]
    service = RecordingService()
    controller = GenericDanmakuController(service)
    item = PlayItem(
        title="第1集",
        url="https://media.example/1.m3u8",
        vod_id="item-1",
        media_title="成何体统",
        danmaku_search_query="成何体统 1集",
        danmaku_candidates=candidates,
    )

    xml = controller.download_danmaku_from_url(
        item,
        "  https://v.qq.com/x/cover/demo/ep1.html  ",
    )

    assert "manual" in xml
    assert service.resolve_calls == ["https://v.qq.com/x/cover/demo/ep1.html"]
    assert item.danmaku_candidates is candidates
    assert item.selected_danmaku_provider == "tencent"
    assert item.selected_danmaku_url == "https://v.qq.com/x/cover/demo/ep1.html"
    assert item.selected_danmaku_title == "第1集"
    assert [(name, url) for name, url, _xml in saved] == [
        ("成何体统 1集", "https://v.qq.com/x/cover/demo/ep1.html"),
        ("成何体统 1集", "item-1"),
    ]


def test_generic_controller_episode_url_failure_preserves_loaded_source(
    monkeypatch,
) -> None:
    class FailingService:
        def provider_key_for_url(self, _page_url: str) -> str:
            return "tencent"

        def resolve_danmu(self, _page_url: str) -> str:
            raise RuntimeError("boom")

    monkeypatch.setattr(
        generic_danmaku_module,
        "load_cached_danmaku_xml",
        lambda _name, _url: "",
    )
    controller = GenericDanmakuController(FailingService())
    candidates = [
        DanmakuSourceGroup(provider="youku", provider_label="优酷", options=[])
    ]
    item = PlayItem(
        title="第1集",
        url="https://media.example/1.m3u8",
        media_title="成何体统",
        danmaku_search_query="成何体统 1集",
        danmaku_candidates=candidates,
        danmaku_xml="<i>old</i>",
        selected_danmaku_provider="youku",
        selected_danmaku_url="https://youku/old",
        selected_danmaku_title="旧来源",
    )

    with pytest.raises(RuntimeError, match="boom"):
        controller.download_danmaku_from_url(
            item,
            "https://v.qq.com/x/cover/demo/ep1.html",
        )

    assert item.danmaku_candidates is candidates
    assert item.danmaku_xml == "<i>old</i>"
    assert item.selected_danmaku_provider == "youku"
    assert item.selected_danmaku_url == "https://youku/old"
    assert item.selected_danmaku_title == "旧来源"


def test_generic_danmaku_controller_delegates_episode_offset(tmp_path: Path) -> None:
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    controller = GenericDanmakuController(object(), danmaku_preference_store=store)
    item = PlayItem(
        title="第12集",
        url="",
        media_title="剑来",
        selected_danmaku_provider="tencent",
    )

    controller.save_danmaku_offset(item, -2.5, playlist=[item])

    assert controller.load_danmaku_offset(item, playlist=[item]) == -2.5
    assert item.danmaku_offset_seconds == -2.5


def test_generic_danmaku_default_episode_label_falls_back_for_variety_collection() -> None:
    # 综艺合集按"第N集"命名（无期号/完整日期）时，集数标签要回退到常规集数解析，
    # 否则弹幕源对话框填不出集数、搜索退化为纯标题查询。
    playlist = [
        PlayItem(
            title=f"第{index:02d}集",
            url=f"https://media.example/{index}.m3u8",
            media_title="奔跑吧",
            type_name="综艺",
            index=index - 1,
        )
        for index in range(1, 4)
    ]

    assert generic_danmaku_module._default_episode_label(playlist[0], playlist) == "1集"


def test_generic_danmaku_default_episode_label_keeps_hint_only_variety_without_episode() -> None:
    item = PlayItem(title="纯享版", url="https://media.example/1.m3u8", media_title="奔跑吧", type_name="综艺")
    playlist = [item, PlayItem(title="第2期", url="https://media.example/2.m3u8", media_title="奔跑吧", type_name="综艺")]

    assert generic_danmaku_module._default_episode_label(item, playlist) == ""


def test_generic_danmaku_default_episode_label_keeps_filename_anchor_for_single_item() -> None:
    # 网盘单文件条目（"288 4K.mkv"）的集数读自文件名而非播放位置：保留锚点，
    # 否则查询退化为纯标题，被同名近似作品夺魁（2026-10-18 完美世界 →
    # 优酷"完美的世界"事故）。
    item = PlayItem(
        title="288 4K.mkv",
        url="http://127.0.0.1:44399/driver/c3d237201e2b324f",
        media_title="完美世界",
    )

    assert generic_danmaku_module._default_episode_label(item, [item]) == "288集"


@pytest.mark.parametrize(
    "title",
    ["300 (2006).mkv", "1917.mkv", "2012.mkv", "阿凡达.mkv"],
)
def test_generic_danmaku_default_episode_label_drops_anchor_for_movie_like_single_items(title: str) -> None:
    # 年份括注、四位数字纯片名（"1917"/"2012"是电影）与无可解析数字的单文件
    # 条目保持标题-only：集数锚点只留给可信的剧集文件名。
    item = PlayItem(
        title=title, url="http://127.0.0.1:44399/driver/abc", media_title="某电影"
    )

    assert generic_danmaku_module._default_episode_label(item, [item]) == ""


def _use_tmp_danmaku_cache(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(generic_danmaku_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(generic_danmaku_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        generic_danmaku_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        generic_danmaku_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )


def test_generic_danmaku_controller_passes_series_preference_to_search(monkeypatch, tmp_path: Path) -> None:
    # 已保存的系列源偏好（provider/page_url）要传给搜索服务参与默认候选挑选，
    # 否则一次正确选择无法钉住后续同系列条目。

    class RecordingDanmakuService:
        def __init__(self) -> None:
            self.search_calls: list[dict[str, object]] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> DanmakuSourceSearchResult:
            del reg_src, media_duration_seconds, provider_filter
            self.search_calls.append(
                {
                    "name": name,
                    "preferred_provider": preferred_provider,
                    "preferred_page_url": preferred_page_url,
                }
            )
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[
                            DanmakuSourceOption(
                                provider="tencent", name="完美世界 288集", url="https://v.qq.com/288"
                            )
                        ],
                    )
                ],
                default_option_url="https://v.qq.com/288",
                default_provider="tencent",
            )

    _use_tmp_danmaku_cache(monkeypatch, tmp_path)
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    store.save(
        DanmakuSeriesPreference(
            series_key="完美世界",
            provider="tencent",
            page_url="https://v.qq.com/x/cover/mcv8hkc8zk8lnov/k4100ss0urf.html",
            title="完美世界 167集",
        )
    )
    service = RecordingDanmakuService()
    controller = GenericDanmakuController(service, danmaku_preference_store=store)
    item = PlayItem(
        title="288 4K.mkv",
        url="http://127.0.0.1:44399/driver/c3d237201e2b324f",
        vod_id="driver-c3d2",
        media_title="完美世界",
    )

    controller.refresh_danmaku_sources(item, playlist=[item], force_refresh=True)

    assert service.search_calls == [
        {
            "name": "完美世界 288集",
            "preferred_provider": "tencent",
            "preferred_page_url": "https://v.qq.com/x/cover/mcv8hkc8zk8lnov/k4100ss0urf.html",
        }
    ]


def test_generic_danmaku_controller_cached_restore_reranks_with_series_preference(monkeypatch, tmp_path: Path) -> None:
    # 缓存恢复路径同样要带偏好重排：缓存里存下的默认候选（可能是错误选择）
    # 不应原样复出，偏好源应有机会顶上。

    class RecordingRerankService:
        def __init__(self) -> None:
            self.rerank_calls: list[dict[str, object]] = []

        def rerank_danmaku_source_search_result(
            self,
            result: DanmakuSourceSearchResult,
            *,
            query_name: str = "",
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ) -> DanmakuSourceSearchResult:
            del reg_src, media_duration_seconds
            self.rerank_calls.append(
                {
                    "query_name": query_name,
                    "preferred_provider": preferred_provider,
                    "preferred_page_url": preferred_page_url,
                }
            )
            return result

    _use_tmp_danmaku_cache(monkeypatch, tmp_path)
    danmaku_cache_module.save_cached_danmaku_source_search_result(
        "完美世界 288集",
        "",
        DanmakuSourceSearchResult(
            groups=[
                DanmakuSourceGroup(
                    provider="youku",
                    provider_label="优酷",
                    options=[
                        DanmakuSourceOption(
                            provider="youku", name="第1集 完美的世界", url="https://v.youku.com/wrong"
                        )
                    ],
                )
            ],
            default_option_url="https://v.youku.com/wrong",
            default_provider="youku",
        ),
    )
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    store.save(
        DanmakuSeriesPreference(
            series_key="完美世界",
            provider="tencent",
            page_url="https://v.qq.com/x/cover/mcv8hkc8zk8lnov/k4100ss0urf.html",
            title="完美世界 167集",
        )
    )
    service = RecordingRerankService()
    controller = GenericDanmakuController(service, danmaku_preference_store=store)
    item = PlayItem(
        title="288 4K.mkv",
        url="http://127.0.0.1:44399/driver/c3d237201e2b324f",
        vod_id="driver-c3d2",
        media_title="完美世界",
    )

    assert controller.load_cached_danmaku_sources(item, playlist=[item]) is True
    assert service.rerank_calls == [
        {
            "query_name": "完美世界 288集",
            "preferred_provider": "tencent",
            "preferred_page_url": "https://v.qq.com/x/cover/mcv8hkc8zk8lnov/k4100ss0urf.html",
        }
    ]


def test_generic_danmaku_controller_switch_saves_series_source_preference(monkeypatch, tmp_path: Path) -> None:
    # generic 路径的下载结果要落库为系列偏好，后续同系列搜索/缓存恢复才能
    # 钉住用户实际使用的源。

    class ResolveDanmakuService:
        def resolve_danmu(self, page_url: str) -> str:
            del page_url
            return '<i><d p="1,1,25,16777215">hi</d></i>'

    monkeypatch.setattr(generic_danmaku_module, "load_cached_danmaku_xml", lambda _name, _url: "")
    monkeypatch.setattr(generic_danmaku_module, "save_cached_danmaku_xml", lambda _name, _url, _xml: None)
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    controller = GenericDanmakuController(ResolveDanmakuService(), danmaku_preference_store=store)
    item = PlayItem(
        title="第1集",
        url="https://media.example/1.m3u8",
        vod_id="item-1",
        media_title="成何体统",
        danmaku_search_title="成何体统",
        danmaku_search_query="成何体统 1集",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[
                    DanmakuSourceOption(
                        provider="tencent", name="成何体统 第1集", url="https://v.qq.com/demo"
                    )
                ],
            )
        ],
    )

    controller.switch_danmaku_source(item, "https://v.qq.com/demo")

    saved = store.load("成何体统")
    assert saved is not None
    assert saved.provider == "tencent"
    assert saved.page_url == "https://v.qq.com/demo"
    assert saved.search_title == "成何体统"


def test_generic_danmaku_controller_refreshes_sources_with_media_title_and_episode(monkeypatch, tmp_path: Path) -> None:
    class RecordingDanmakuService:
        def __init__(self) -> None:
            self.search_calls: list[tuple[str, str, str]] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> DanmakuSourceSearchResult:
            del preferred_provider, preferred_page_url, media_duration_seconds
            self.search_calls.append((name, reg_src, provider_filter))
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[
                    DanmakuSourceOption(
                        provider="tencent", name="成何体统 第1集", url="https://v.qq.com/demo"
                    )
                ],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(generic_danmaku_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(generic_danmaku_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        generic_danmaku_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        generic_danmaku_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    service = RecordingDanmakuService()
    controller = GenericDanmakuController(service)
    item = PlayItem(title="第1集", url="https://media.example/1.m3u8", vod_id="item-1", media_title="成何体统 (2026)", index=0)

    controller.refresh_danmaku_sources(item, playlist=[item], force_refresh=True, provider_filter="tencent")

    assert service.search_calls == [("成何体统 (2026) 1集", "item-1", "tencent")]
    assert item.danmaku_search_title == "成何体统 (2026)"
    assert item.danmaku_search_episode == "1集"
    assert item.danmaku_search_query == "成何体统 (2026) 1集"
    assert item.selected_danmaku_provider == "tencent"
    assert item.selected_danmaku_url == "https://v.qq.com/demo"
    assert item.selected_danmaku_title == "成何体统 第1集"


def test_generic_danmaku_controller_refresh_emits_log_events(monkeypatch, tmp_path: Path) -> None:
    class RecordingDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> DanmakuSourceSearchResult:
            del reg_src, preferred_provider, preferred_page_url, media_duration_seconds, provider_filter
            assert name == "成何体统 1集"
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[
                    DanmakuSourceOption(
                        provider="tencent", name="成何体统 第1集", url="https://v.qq.com/demo"
                    )
                ],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(generic_danmaku_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(generic_danmaku_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        generic_danmaku_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        generic_danmaku_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    controller = GenericDanmakuController(RecordingDanmakuService())
    logs: list[str] = []
    controller.set_danmaku_log_handler(logs.append)
    item = PlayItem(title="第1集", url="https://media.example/1.m3u8", vod_id="item-1", media_title="成何体统")

    controller.refresh_danmaku_sources(item, playlist=[item], force_refresh=True)

    assert logs == [
        "弹幕搜索中: 成何体统 1集",
        "弹幕搜索成功: 找到 1 个候选",
    ]


def test_generic_danmaku_controller_auto_resolves_uncached_episode(monkeypatch) -> None:
    class RecordingDanmakuService:
        def __init__(self) -> None:
            self.search_calls: list[str] = []
            self.resolve_calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            media_duration_seconds: int = 0,
        ) -> DanmakuSourceSearchResult:
            del reg_src, media_duration_seconds
            self.search_calls.append(name)
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[
                            DanmakuSourceOption(
                                provider="tencent",
                                name="百花杀 15集",
                                url="https://v.qq.com/ep15",
                            )
                        ],
                    )
                ],
                default_option_url="https://v.qq.com/ep15",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str, option=None) -> str:
            del option
            self.resolve_calls.append(page_url)
            return '<i><d p="1,1,25,16777215">第十五集</d></i>'

    monkeypatch.setattr(generic_danmaku_module, "load_cached_danmaku_source_search_result", lambda *_args: None)
    monkeypatch.setattr(generic_danmaku_module, "save_cached_danmaku_source_search_result", lambda *_args: None)
    monkeypatch.setattr(generic_danmaku_module, "load_cached_danmaku_xml", lambda *_args: "")
    monkeypatch.setattr(generic_danmaku_module, "save_cached_danmaku_xml", lambda *_args: None)
    service = RecordingDanmakuService()
    controller = GenericDanmakuController(service)
    logs: list[str] = []
    controller.set_danmaku_log_handler(logs.append)
    item = PlayItem(
        title="15(5.41 GB)",
        url="https://media.example/15.mp4",
        vod_id="episode-15",
        media_title="百花杀",
        index=14,
    )
    playlist = [
        PlayItem(title=f"{index:02d}(5 GB)", url=f"https://media.example/{index}.mp4", index=index - 1)
        for index in range(1, 15)
    ] + [item]

    assert controller.auto_resolve_danmaku(item, playlist=playlist) is True

    assert service.search_calls == ["百花杀 15集"]
    assert service.resolve_calls == ["https://v.qq.com/ep15"]
    assert "第十五集" in item.danmaku_xml
    assert logs == [
        "弹幕搜索中: 百花杀 15集",
        "弹幕搜索成功: 找到 1 个候选",
        "弹幕下载中: 腾讯 - 百花杀 15集",
        "弹幕下载成功: 1 条弹幕",
    ]


def test_generic_danmaku_controller_prefetches_next_episode(monkeypatch) -> None:
    controller = GenericDanmakuController(object())
    logs: list[str] = []
    controller.set_danmaku_log_handler(logs.append)
    next_item = PlayItem(
        title="第2集",
        url="https://media.example/2.mp4",
        media_title="百花杀",
        index=1,
    )
    playlist = [
        PlayItem(title="第1集", url="https://media.example/1.mp4", media_title="百花杀", index=0),
        next_item,
    ]
    calls: list[PlayItem] = []

    def fake_auto_resolve(item: PlayItem, playlist: list[PlayItem] | None = None, **_kwargs) -> bool:
        assert playlist is not None
        calls.append(item)
        item.danmaku_xml = '<i><d p="1,1,25,16777215">第2集</d></i>'
        return True

    monkeypatch.setattr(controller, "auto_resolve_danmaku", fake_auto_resolve)

    controller.prefetch_next_episode_danmaku(next_item, playlist)

    assert calls == [next_item]
    assert logs == [
        "弹幕预下载中: 百花杀 2集",
        "弹幕预下载成功: 1 条弹幕",
    ]


def test_generic_danmaku_controller_refresh_emits_all_actual_search_queries(monkeypatch, tmp_path: Path) -> None:
    class RecordingDanmakuService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> DanmakuSourceSearchResult:
            del reg_src, preferred_provider, preferred_page_url, media_duration_seconds, provider_filter
            self.calls.append(name)
            if name == "成何体统 1集":
                return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")
            if name == "成何体统":
                return DanmakuSourceSearchResult(
                    groups=[
                        DanmakuSourceGroup(
                            provider="iqiyi",
                            provider_label="爱奇艺",
                            options=[DanmakuSourceOption(provider="iqiyi", name="成何体统 第1集", url="https://www.iqiyi.com/v_ep1.html")],
                        )
                    ],
                    default_option_url="https://www.iqiyi.com/v_ep1.html",
                    default_provider="iqiyi",
                )
            raise AssertionError(name)

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(generic_danmaku_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(generic_danmaku_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        generic_danmaku_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        generic_danmaku_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    service = RecordingDanmakuService()
    controller = GenericDanmakuController(service)
    logs: list[str] = []
    controller.set_danmaku_log_handler(logs.append)
    item = PlayItem(title="第1集", url="https://media.example/1.m3u8", vod_id="item-1", media_title="成何体统")

    controller.refresh_danmaku_sources(item, playlist=[item], force_refresh=True)

    assert service.calls == ["成何体统 1集", "成何体统"]
    assert logs == [
        "弹幕搜索中: 成何体统 1集",
        "弹幕搜索中: 成何体统",
        "弹幕搜索成功: 找到 1 个候选",
    ]


def test_generic_danmaku_controller_switches_to_cached_xml_without_refetch(monkeypatch, tmp_path: Path) -> None:
    class FailingResolveDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> DanmakuSourceSearchResult:
            del name, reg_src, preferred_provider, preferred_page_url, media_duration_seconds, provider_filter
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="成何体统", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str, option=None) -> str:
            raise AssertionError(f"should use cached xml instead of resolving {page_url!r} with {option!r}")

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(generic_danmaku_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(generic_danmaku_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        generic_danmaku_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        generic_danmaku_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    controller = GenericDanmakuController(FailingResolveDanmakuService())
    item = PlayItem(title="正片", url="https://media.example/movie.m3u8", vod_id="item-1", media_title="成何体统 (2026)")
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0,1,25,16777215">第一条</d></i>'

    controller.refresh_danmaku_sources(item, playlist=[item], force_refresh=True)
    danmaku_cache_module.save_cached_danmaku_xml(item.danmaku_search_query, "https://v.qq.com/demo", xml_text)

    resolved = controller.switch_danmaku_source(item, "https://v.qq.com/demo")

    assert resolved == xml_text
    assert item.danmaku_xml == xml_text


def test_generic_danmaku_controller_switch_emits_log_events(monkeypatch, tmp_path: Path) -> None:
    class ResolveDanmakuService:
        def resolve_danmu(self, page_url: str, option=None) -> str:
            del option
            assert page_url == "https://v.qq.com/demo"
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="0,1,25,16777215">第一条</d></i>'

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(generic_danmaku_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(generic_danmaku_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    controller = GenericDanmakuController(ResolveDanmakuService())
    logs: list[str] = []
    controller.set_danmaku_log_handler(logs.append)
    item = PlayItem(
        title="第1集",
        url="https://media.example/1.m3u8",
        vod_id="item-1",
        media_title="成何体统",
        danmaku_search_query="成何体统 1集",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[
                    DanmakuSourceOption(
                        provider="tencent", name="成何体统 第1集", url="https://v.qq.com/demo"
                    )
                ],
            )
        ],
    )

    controller.switch_danmaku_source(item, "https://v.qq.com/demo")

    assert logs == [
        "弹幕下载中: 腾讯 - 成何体统 第1集",
        "弹幕下载成功: 1 条弹幕",
    ]


def test_generic_danmaku_controller_skips_caching_other_fallback_only_result(monkeypatch) -> None:
    class OtherFallbackService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> DanmakuSourceSearchResult:
            del name, reg_src, preferred_provider, preferred_page_url, media_duration_seconds, provider_filter
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="other",
                        provider_label="other",
                        options=[DanmakuSourceOption(provider="other", name="冷门剧 1集", url="https://v.qq.com/x/cover/xyz/abc.html")],
                    )
                ],
                default_option_url="https://v.qq.com/x/cover/xyz/abc.html",
                default_provider="other",
            )

    saved: list[str] = []
    monkeypatch.setattr(generic_danmaku_module, "load_cached_danmaku_source_search_result", lambda *_args: None)
    monkeypatch.setattr(
        generic_danmaku_module,
        "save_cached_danmaku_source_search_result",
        lambda name, reg_src, result: saved.append(name),
    )
    controller = GenericDanmakuController(OtherFallbackService())
    item = PlayItem(title="第1集", url="https://media.example/1.m3u8", vod_id="item-1", media_title="冷门剧", index=0)

    controller.refresh_danmaku_sources(item, playlist=[item], force_refresh=True)

    # 纯 other 兜底结果不写共享缓存，但当前条目仍可兜底展示候选。
    assert saved == []
    assert [group.provider for group in item.danmaku_candidates] == ["other"]


def test_generic_danmaku_controller_rejects_stale_other_fallback_cache(monkeypatch) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.search_calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> DanmakuSourceSearchResult:
            del reg_src, preferred_provider, preferred_page_url, media_duration_seconds, provider_filter
            self.search_calls.append(name)
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

    poisoned = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="other",
                provider_label="other",
                options=[DanmakuSourceOption(provider="other", name="冷门剧 1集", url="https://pan.baidu.com/s/1ZuAV?pwd=9527")],
            )
        ],
        default_option_url="https://pan.baidu.com/s/1ZuAV?pwd=9527",
        default_provider="other",
    )
    monkeypatch.setattr(generic_danmaku_module, "load_cached_danmaku_source_search_result", lambda *_args: poisoned)
    monkeypatch.setattr(generic_danmaku_module, "save_cached_danmaku_source_search_result", lambda *_args: None)
    service = RecordingService()
    controller = GenericDanmakuController(service)
    item = PlayItem(title="第1集", url="https://media.example/1.m3u8", vod_id="item-1", media_title="冷门剧", index=0)

    controller.refresh_danmaku_sources(item, playlist=[item])

    # 历史污染缓存（other 候选指向别的 reg_src）被拒用，回退到新搜索。
    assert service.search_calls == ["冷门剧 1集", "冷门剧"]


def _season_ladder_monkeypatch(monkeypatch) -> None:
    monkeypatch.setattr(generic_danmaku_module, "load_cached_danmaku_source_search_result", lambda *_args: None)
    monkeypatch.setattr(generic_danmaku_module, "save_cached_danmaku_source_search_result", lambda *_args: None)
    monkeypatch.setattr(generic_danmaku_module, "load_cached_danmaku_xml", lambda *_args: "")
    monkeypatch.setattr(generic_danmaku_module, "save_cached_danmaku_xml", lambda *_args: None)


def _make_season_suffixed_item() -> PlayItem:
    item = PlayItem(
        title="第177集 慕兰之战01",
        url="https://media.example/177.mp4",
        vod_id="drive-episode-177",
        media_title="凡人修仙传 年番4",
        index=176,
    )
    item.danmaku_search_episode = "177集"
    return item


def test_generic_danmaku_controller_falls_back_to_season_stripped_query(monkeypatch) -> None:
    class SeasonSuffixService:
        def __init__(self) -> None:
            self.search_calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            media_duration_seconds: int = 0,
        ) -> DanmakuSourceSearchResult:
            del reg_src, media_duration_seconds
            self.search_calls.append(name)
            if name != "凡人修仙传 177集":
                return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="bilibili",
                        provider_label="B站",
                        options=[
                            DanmakuSourceOption(
                                provider="bilibili",
                                name="《凡人修仙传》第177话 慕兰之战01",
                                url="https://www.bilibili.com/bangumi/play/ep177",
                            )
                        ],
                    )
                ],
                default_option_url="https://www.bilibili.com/bangumi/play/ep177",
                default_provider="bilibili",
            )

    _season_ladder_monkeypatch(monkeypatch)
    controller = GenericDanmakuController(SeasonSuffixService())
    logs: list[str] = []
    controller.set_danmaku_log_handler(logs.append)
    item = _make_season_suffixed_item()

    controller.refresh_danmaku_sources(item, playlist=[item], force_refresh=True)

    # 网盘季名"年番4"整季 0 结果 → 剥季名保集数锚点重搜,命中 B站连续集数条目。
    assert controller._danmaku_service.search_calls == [
        "凡人修仙传 年番4 177集",
        "凡人修仙传 177集",
    ]
    assert logs == [
        "弹幕搜索中: 凡人修仙传 年番4 177集",
        "弹幕搜索中: 凡人修仙传 177集",
        "弹幕搜索成功: 找到 1 个候选",
    ]
    assert item.selected_danmaku_url == "https://www.bilibili.com/bangumi/play/ep177"
    assert item.selected_danmaku_provider == "bilibili"


def test_generic_danmaku_controller_season_ladder_exhausts_to_suffixed_title(monkeypatch) -> None:
    class EmptyResultService:
        def __init__(self) -> None:
            self.search_calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            media_duration_seconds: int = 0,
        ) -> DanmakuSourceSearchResult:
            del reg_src, media_duration_seconds
            self.search_calls.append(name)
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

    _season_ladder_monkeypatch(monkeypatch)
    controller = GenericDanmakuController(EmptyResultService())
    logs: list[str] = []
    controller.set_danmaku_log_handler(logs.append)
    item = _make_season_suffixed_item()

    controller.refresh_danmaku_sources(item, playlist=[item], force_refresh=True)

    # 全链降级:剥季名+集数 → 剥季名 → 纯标题,耗尽后仍如实报 0 候选。
    assert controller._danmaku_service.search_calls == [
        "凡人修仙传 年番4 177集",
        "凡人修仙传 177集",
        "凡人修仙传",
        "凡人修仙传 年番4",
    ]
    assert logs[-1] == "弹幕搜索成功: 找到 0 个候选"
    assert item.danmaku_candidates == []


def test_generic_danmaku_controller_respects_user_query_override_without_ladder(monkeypatch) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.search_calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            media_duration_seconds: int = 0,
        ) -> DanmakuSourceSearchResult:
            del reg_src, media_duration_seconds
            self.search_calls.append(name)
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

    _season_ladder_monkeypatch(monkeypatch)
    controller = GenericDanmakuController(RecordingService())
    item = _make_season_suffixed_item()

    controller.refresh_danmaku_sources(item, query_override="自定义 查询", playlist=[item], force_refresh=True)

    # 手动指定的查询是显式意图:0 结果也不悄悄换词重搜。
    assert controller._danmaku_service.search_calls == ["自定义 查询"]
