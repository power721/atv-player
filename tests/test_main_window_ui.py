import json
import threading
import time
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtWidgets import QApplication, QWidget

import atv_player.danmaku.cache as danmaku_cache_module
import atv_player.danmaku.direct_parse as direct_parse_danmaku_module
import atv_player.plugins.controller as spider_controller_module
import atv_player.ui.main_window as main_window_module
from atv_player.controllers.following_controller import FollowingDetailView
from atv_player.controllers.media_detail_controller import MediaDetailEpisode
from atv_player.controllers.media_detail_controller import MediaDetailIdentity
from atv_player.controllers.media_detail_controller import MediaDetailLookup
from atv_player.controllers.media_detail_controller import MediaDetailPerson
from atv_player.controllers.media_detail_controller import MediaDetailRecommendation
from atv_player.controllers.media_detail_controller import MediaDetailView
from atv_player.controllers.pagination import PageInfo
from atv_player.controllers.player_controller import PlayerController, PlayerSession
from atv_player.danmaku.models import (
    DanmakuSourceGroup,
    DanmakuSourceOption,
    DanmakuSourceSearchResult,
)
from atv_player.following_models import (
    FollowingDetailSnapshot,
    FollowingEpisode,
    FollowingRecord,
    FollowingSeason,
    FollowingSourceBinding,
)
from atv_player.models import (
    AppConfig,
    FavoriteCardItem,
    FavoriteRecord,
    HistoryRecord,
    OpenPlayerRequest,
    PlaybackDetailField,
    PlaybackDetailFieldAction,
    PlayItem,
    VodItem,
)
from atv_player.player.startup import PlaybackStartupStage
from atv_player.plugins.controller import SpiderPluginController
from atv_player.ui.main_window import (
    MainWindow,
    load_tencent_hot_search_sections,
    load_tencent_hot_searches,
)
from atv_player.ui.following_episode_browser import EPISODE_ROLE
from atv_player.ui.player_window import PlayerWindow


class FakeStaticController:
    def load_categories(self):
        return []

    def load_items(self, category_id: str, page: int, filters=None):
        return [], 0


class FakeSpiderController:
    def __init__(self, name: str) -> None:
        self.name = name
        self.open_calls: list[str] = []
        self.folder_calls: list[str] = []

    def load_categories(self):
        return []

    def load_items(self, category_id: str, page: int):
        return [], 0

    def load_folder_items(self, vod_id: str):
        self.folder_calls.append(vod_id)
        return [VodItem(vod_id="child-1", vod_name="子目录", vod_tag="folder")], 1

    def build_request(self, vod_id: str):
        self.open_calls.append(vod_id)
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name=self.name),
            playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )


def test_main_window_plugin_folder_click_loads_folder_in_plugin_page(qtbot, monkeypatch) -> None:
    controller = FakeSpiderController("目录插件")
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[{"id": "plugin-1", "title": "目录插件", "controller": controller, "search_enabled": False}],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)
    shown = []
    monkeypatch.setattr(window._plugin_pages[0][0], "show_items", lambda items, total, **kwargs: shown.append((items, total, kwargs)))

    window._open_spider_item(
        controller,
        "plugin-1",
        VodItem(vod_id="folder-1", vod_name="分区", vod_tag="folder"),
    )

    qtbot.waitUntil(lambda: controller.folder_calls == ["folder-1"] and len(shown) == 1, timeout=1000)
    assert controller.open_calls == []
    assert shown[0][1] == 1
    assert shown[0][2]["empty_message"] == "当前文件夹暂无内容"
    assert window._plugin_pages[0][0]._folder_breadcrumbs[-1] == {
        "id": "folder-1",
        "label": "分区",
        "kind": "folder",
    }


def test_main_window_classic_plugin_folder_click_loads_folder_in_classic_grid(qtbot, monkeypatch) -> None:
    controller = FakeSpiderController("目录插件")
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(home_mode="classic", last_selected_tab="plugin:plugin-1"),
        spider_plugins=[{"id": "plugin-1", "title": "目录插件", "controller": controller, "search_enabled": False}],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)
    classic_shown = []
    hidden_plugin_shown = []
    monkeypatch.setattr(
        window._classic_home_page.grid_page,
        "show_items",
        lambda items, total, **kwargs: classic_shown.append((items, total, kwargs)),
    )
    monkeypatch.setattr(
        window._plugin_pages[0][0],
        "show_items",
        lambda items, total, **kwargs: hidden_plugin_shown.append((items, total, kwargs)),
    )

    window._handle_classic_item_open(VodItem(vod_id="folder-1", vod_name="分区", vod_tag="folder"))

    qtbot.waitUntil(lambda: controller.folder_calls == ["folder-1"] and len(classic_shown) == 1, timeout=1000)
    assert hidden_plugin_shown == []
    assert classic_shown[0][1] == 1
    assert window._classic_home_page.grid_page._folder_breadcrumbs[-1] == {
        "id": "folder-1",
        "label": "分区",
        "kind": "folder",
    }


def test_main_window_plugin_msearch_item_starts_global_search_without_player(qtbot, monkeypatch) -> None:
    controller = FakeSpiderController("搜索插件")
    player_windows: list[object] = []
    monkeypatch.setattr(main_window_module, "PlayerWindow", lambda *args, **kwargs: player_windows.append(object()))
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[{"id": "plugin-1", "title": "搜索插件", "controller": controller, "search_enabled": True}],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)
    searches: list[str] = []
    monkeypatch.setattr(window, "_start_global_search", lambda **_kwargs: searches.append(window.global_search_edit.text()))

    window._open_spider_item(controller, "plugin-1", VodItem(vod_id="msearch:三体", vod_name="三体"))

    assert searches == ["三体"]
    assert controller.open_calls == []
    assert player_windows == []
    assert window.player_window is None


def test_main_window_classic_plugin_msearch_item_starts_global_search(qtbot, monkeypatch) -> None:
    controller = FakeSpiderController("搜索插件")
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(home_mode="classic", last_selected_tab="plugin:plugin-1"),
        spider_plugins=[{"id": "plugin-1", "title": "搜索插件", "controller": controller, "search_enabled": True}],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)
    searches: list[str] = []
    monkeypatch.setattr(window, "_start_global_search", lambda **_kwargs: searches.append(window.global_search_edit.text()))

    window._handle_classic_item_open(VodItem(vod_id="msearch:庆余年", vod_name="庆余年"))

    assert searches == ["庆余年"]
    assert controller.open_calls == []


class FakePluginManager:
    def __init__(self) -> None:
        self.dialog_opened = 0
        self.plugins = [
            SimpleNamespace(id=1, display_name="插件1", enabled=True, config_text="token=1\n", sort_order=0),
            SimpleNamespace(id=2, display_name="插件2", enabled=True, config_text="token=2\n", sort_order=1),
            SimpleNamespace(id=3, display_name="插件3", enabled=True, config_text="token=3\n", sort_order=2),
        ]
        self.rename_calls: list[tuple[int, str]] = []
        self.config_calls: list[tuple[int, str]] = []
        self.toggle_calls: list[tuple[int, bool]] = []
        self.refresh_calls: list[int] = []
        self.load_plugins_calls: list[list[str]] = []

    def list_plugins(self):
        return list(self.plugins)

    def load_plugins(self, plugin_ids, drive_detail_loader=None, offline_download_detail_loader=None):
        requested = {str(plugin_id) for plugin_id in plugin_ids}
        self.load_plugins_calls.append(sorted(requested))
        definitions = []
        for plugin in self.plugins:
            if not plugin.enabled or str(plugin.id) not in requested:
                continue
            definitions.append(
                {
                    "id": str(plugin.id),
                    "title": plugin.display_name,
                    "controller": FakeSpiderController(plugin.display_name),
                    "search_enabled": True,
                }
            )
        return definitions

    def rename_plugin(self, plugin_id: int, display_name: str) -> None:
        self.rename_calls.append((plugin_id, display_name))
        for plugin in self.plugins:
            if plugin.id == plugin_id:
                plugin.display_name = display_name
                return

    def set_plugin_config(self, plugin_id: int, config_text: str) -> None:
        self.config_calls.append((plugin_id, config_text))
        for plugin in self.plugins:
            if plugin.id == plugin_id:
                plugin.config_text = config_text
                return

    def set_plugin_enabled(self, plugin_id: int, enabled: bool) -> None:
        self.toggle_calls.append((plugin_id, enabled))
        for plugin in self.plugins:
            if plugin.id == plugin_id:
                plugin.enabled = enabled
                return

    def refresh_plugin(self, plugin_id: int) -> None:
        self.refresh_calls.append(plugin_id)


class WidthAwarePluginManager(FakePluginManager):
    pass


class CountingSpiderController(FakeSpiderController):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.load_calls = 0

    def load_categories(self):
        self.load_calls += 1
        return []


class FakePlayerController:
    def create_session(
        self,
        vod,
        playlist,
        clicked_index: int,
        playlists=None,
        playlist_index: int = 0,
        source_groups=None,
        source_group_index: int = 0,
        source_index: int = 0,
        source_kind: str = "",
        source_key: str = "",
        detail_resolver=None,
        resolved_vod_by_id=None,
        use_local_history=True,
        restore_history=False,
        playback_loader=None,
        async_playback_loader=False,
        detail_action_runner=None,
        detail_field_runner=None,
        metadata_hydrator=None,
        metadata_scrape_service=None,
        metadata_binding_repository=None,
        episode_title_enhancer=None,
        danmaku_controller=None,
        playback_progress_reporter=None,
        playback_stopper=None,
        playback_history_loader=None,
        playback_history_saver=None,
        initial_log_message="",
        is_placeholder=False,
    ):
        return {
            "vod": vod,
            "playlist": playlist,
            "clicked_index": clicked_index,
            "playlists": playlists,
            "playlist_index": playlist_index,
            "source_groups": source_groups,
            "source_group_index": source_group_index,
            "source_index": source_index,
            "source_kind": source_kind,
            "source_key": source_key,
            "restore_history": restore_history,
            "async_playback_loader": async_playback_loader,
            "detail_action_runner": detail_action_runner,
            "detail_field_runner": detail_field_runner,
            "metadata_hydrator": metadata_hydrator,
            "metadata_scrape_service": metadata_scrape_service,
            "metadata_binding_repository": metadata_binding_repository,
            "episode_title_enhancer": episode_title_enhancer,
            "danmaku_controller": danmaku_controller,
            "playback_history_loader": playback_history_loader,
            "playback_history_saver": playback_history_saver,
            "initial_log_message": initial_log_message,
            "is_placeholder": is_placeholder,
        }


class DummyHistoryController:
    def list_records(self):
        return []


class FakeFavoritesController:
    def __init__(self) -> None:
        self.add_calls: list[dict[str, object]] = []
        self.remove_calls: list[list[FavoriteRecord]] = []
        self.load_calls: list[tuple[int, int, str]] = []

    def load_page(self, *, page: int, size: int, keyword: str):
        self.load_calls.append((page, size, keyword))
        return [], 0

    def is_favorited(self, *, source_kind: str, source_key: str, vod_id: str) -> bool:
        del source_kind, source_key, vod_id
        return False

    def add_favorite(self, payload: dict[str, object]) -> None:
        self.add_calls.append(payload)

    def remove_favorite(self, records: list[FavoriteRecord]) -> None:
        self.remove_calls.append(list(records))

    def clear_filtered(self, *, keyword: str) -> None:
        del keyword


class FakeFollowingController:
    def __init__(self) -> None:
        self.load_calls: list[tuple[int, int, str, bool]] = []
        self.cleared: list[int] = []
        self.snoozed: list[int] = []
        self.dismissed_until_next: list[int] = []
        self.added_candidates: list[object] = []

    def load_page(self, *, page: int, size: int, keyword: str, only_updates: bool):
        self.load_calls.append((page, size, keyword, only_updates))
        return [], 0

    def load_homepage_prompts(self):
        return [
            FollowingRecord(
                id=1,
                title="凡人修仙传",
                provider="bangumi",
                provider_id="subject:1",
                latest_episode=128,
                new_episode_count=1,
                homepage_prompt_pending=True,
            )
        ]

    def load_detail(self, following_id: int):
        return FollowingDetailView(
            record=FollowingRecord(
                id=following_id,
                title="凡人修仙传",
                provider="bangumi",
                provider_id="subject:1",
                latest_episode=128,
                new_episode_count=1,
            ),
            snapshot=FollowingDetailSnapshot(following_id=following_id),
        )

    def clear_homepage_prompt(self, following_id: int) -> None:
        self.cleared.append(following_id)

    def snooze_prompt(self, following_id: int) -> None:
        self.snoozed.append(following_id)

    def dismiss_prompt_until_next_episode(self, following_id: int) -> None:
        self.dismissed_until_next.append(following_id)

    def add_candidate(self, candidate, **kwargs):
        del kwargs
        self.added_candidates.append(candidate)
        return FollowingRecord(id=99, title=getattr(candidate, "title", ""), provider="tmdb", provider_id="tv:1399")


class FakeHeatController:
    def __init__(self) -> None:
        self.media_events: list[tuple[str, object, dict[str, object]]] = []
        self.searches: list[tuple[str, str, object]] = []

    def record_search(self, query, *, source_kind="global_search", media=None) -> None:
        self.searches.append((query, source_kind, media))

    def record_media_event(self, event_type: str, media, *, context=None) -> None:
        self.media_events.append((event_type, media, dict(context or {})))

    def load_recommendations(self, *, limit=24):
        return []


def _spin_until(predicate, timeout_seconds: float = 5.0) -> None:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def _player_window_has_active_danmaku(window: MainWindow) -> bool:
    player_window = window.player_window
    if player_window is None or player_window.session is None or not player_window.session.playlist:
        return False
    if not (0 <= player_window.current_index < len(player_window.session.playlist)):
        return False
    current_item = player_window.session.playlist[player_window.current_index]
    return bool(current_item.danmaku_xml) and bool(getattr(player_window, "_danmaku_active", False))


def test_main_window_uses_custom_title_bar(qtbot) -> None:
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
    )
    qtbot.addWidget(window)

    assert window.title_bar().title_label.text() == "alist-tvbox Desktop Player"
    assert window.title_bar().maximize_button.isHidden() is False


def test_main_window_registers_favorites_tab_and_header_button(qtbot) -> None:
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        favorites_controller=FakeFavoritesController(),
    )
    qtbot.addWidget(window)

    assert window.favorites_button.toolTip() == "我的收藏"
    assert window._tab_key_for_widget(window.favorites_page) == "favorites"


def test_main_window_registers_following_tab_and_header_button(qtbot) -> None:
    following = FakeFollowingController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
    )
    qtbot.addWidget(window)

    assert window.following_page is not None
    assert window.following_button.toolTip() == "我的追更"
    assert window._tab_key_for_widget(window.following_page) == "following"


def test_main_window_passes_config_to_following_detail_page_after_storing_it(qtbot) -> None:
    config = AppConfig(following_episode_display_mode="full")
    following = FakeFollowingController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        config,
        following_controller=following,
    )
    qtbot.addWidget(window)

    assert window.config is config
    assert window.following_detail_page._config is config
    assert window.following_detail_page.episode_browser.display_mode() == "full"


def test_main_window_matches_player_following_by_external_ids(qtbot) -> None:
    class ExternalIdFollowingController(FakeFollowingController):
        def __init__(self) -> None:
            super().__init__()
            self.deleted: list[int] = []
            self.add_from_player_calls: list[dict[str, object]] = []
            self._active = True

        def load_page(self, *, page: int, size: int, keyword: str, only_updates: bool):
            super().load_page(page=page, size=size, keyword=keyword, only_updates=only_updates)
            if not self._active:
                return [], 0
            return [
                SimpleNamespace(
                    record=FollowingRecord(
                        id=7,
                        title="凡人修仙传",
                        provider="bangumi",
                        provider_id="subject:526975",
                        external_ids={"tmdb": "76479", "bangumi": "526975"},
                    )
                )
            ], 1

        def delete(self, following_id: int) -> None:
            self.deleted.append(following_id)
            self._active = False

        def add_from_player(self, **kwargs) -> None:
            self.add_from_player_calls.append(kwargs)

    following = ExternalIdFollowingController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
    )
    qtbot.addWidget(window)

    item = PlayItem(
        title="第1集",
        url="https://media.example/1.m3u8",
        vod_id="vod-1",
        detail_fields=[PlaybackDetailField(label="TMDB ID", value="76479")],
    )
    window.player_window = SimpleNamespace(session=PlayerSession(
        vod=VodItem(
            vod_id="vod-1",
            vod_name="凡人修仙传",
            detail_fields=[
                PlaybackDetailField(label="TMDB ID", value="76479"),
                PlaybackDetailField(label="Bangumi ID", value="526975"),
            ],
        ),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        source_kind="browse",
        source_key="",
    ))

    assert window._player_item_is_followed(item) is True

    window._toggle_player_item_following(item)

    assert following.deleted == [7]
    assert following.add_from_player_calls == []


def test_main_window_adds_failed_player_item_without_marking_current_episode(qtbot) -> None:
    class AddTrackingFollowingController(FakeFollowingController):
        def __init__(self) -> None:
            super().__init__()
            self.add_from_player_calls: list[dict[str, object]] = []

        def add_from_player(self, **kwargs) -> None:
            self.add_from_player_calls.append(kwargs)

    following = AddTrackingFollowingController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
    )
    qtbot.addWidget(window)

    item = PlayItem(title="第112集", url="https://media.example/112.m3u8", vod_id="vod-1")
    window.player_window = SimpleNamespace(
        session=PlayerSession(
            vod=VodItem(vod_id="vod-1", vod_name="成何体统第二季"),
            playlist=[item],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            source_kind="browse",
            source_key="",
        ),
        _startup_state=SimpleNamespace(stage=PlaybackStartupStage.FAILED),
        video=SimpleNamespace(position_seconds=lambda: 0),
    )

    window._toggle_player_item_following(item)

    assert following.add_from_player_calls
    assert following.add_from_player_calls[0]["mark_current_episode"] is False


def test_main_window_reports_heat_following_add_from_player(qtbot) -> None:
    class AddTrackingFollowingController(FakeFollowingController):
        def __init__(self) -> None:
            super().__init__()
            self.add_from_player_calls: list[dict[str, object]] = []

        def add_from_player(self, **kwargs) -> None:
            self.add_from_player_calls.append(kwargs)

    following = AddTrackingFollowingController()
    heat = FakeHeatController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
        heat_controller=heat,
    )
    qtbot.addWidget(window)

    item = PlayItem(title="第1集", url="https://media.example/1.m3u8", vod_id="vod-1")
    window.player_window = SimpleNamespace(
        session=PlayerSession(
            vod=VodItem(
                vod_id="vod-1",
                vod_name="凡人修仙传",
                detail_fields=[PlaybackDetailField(label="Bangumi ID", value="526975")],
            ),
            playlist=[item],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            source_kind="browse",
            source_key="",
        ),
        _startup_state=SimpleNamespace(stage=PlaybackStartupStage.PLAYING),
        video=SimpleNamespace(position_seconds=lambda: 0),
    )

    window._toggle_player_item_following(item)

    assert following.add_from_player_calls
    assert len(heat.media_events) == 1
    event_type, media, context = heat.media_events[0]
    assert event_type == "following_add"
    assert media.title == "凡人修仙传"
    assert context == {"source_kind": "browse", "source_key": ""}


def test_main_window_skips_heat_following_add_without_external_media_id(qtbot) -> None:
    class AddTrackingFollowingController(FakeFollowingController):
        def __init__(self) -> None:
            super().__init__()
            self.add_from_player_calls: list[dict[str, object]] = []

        def add_from_player(self, **kwargs) -> None:
            self.add_from_player_calls.append(kwargs)

    following = AddTrackingFollowingController()
    heat = FakeHeatController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
        heat_controller=heat,
    )
    qtbot.addWidget(window)

    item = PlayItem(title="第1集", url="https://media.example/1.m3u8", vod_id="vod-1")
    window.player_window = SimpleNamespace(
        session=PlayerSession(
            vod=VodItem(vod_id="vod-1", vod_name="未刮削追更"),
            playlist=[item],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            source_kind="browse",
            source_key="",
        ),
        _startup_state=SimpleNamespace(stage=PlaybackStartupStage.PLAYING),
        video=SimpleNamespace(position_seconds=lambda: 0),
    )

    window._toggle_player_item_following(item)

    assert following.add_from_player_calls
    assert heat.media_events == []


def test_main_window_reports_following_progress_only_after_threshold(qtbot) -> None:
    class ReportingFollowingController(FakeFollowingController):
        def __init__(self) -> None:
            super().__init__()
            self.progress_calls: list[tuple[int, int, int]] = []

        def load_page(self, *, page: int, size: int, keyword: str, only_updates: bool):
            super().load_page(page=page, size=size, keyword=keyword, only_updates=only_updates)
            return [
                SimpleNamespace(
                    record=FollowingRecord(
                        id=7,
                        title="凡人修仙传",
                        provider="player",
                        provider_id="browse::vod-1",
                        current_season_number=1,
                        current_episode=1,
                        season_number=1,
                    )
                )
            ], 1

        def record_playback_progress(
            self,
            following_id: int,
            *,
            current_season_number: int,
            current_episode: int,
            position_seconds: int,
        ) -> None:
            del current_season_number
            self.progress_calls.append((following_id, current_episode, position_seconds))

    following = ReportingFollowingController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
    )
    qtbot.addWidget(window)

    item = PlayItem(title="第24集", url="https://media.example/24.m3u8", vod_id="vod-1")
    window.player_window = SimpleNamespace(
        session=PlayerSession(
            vod=VodItem(vod_id="vod-1", vod_name="凡人修仙传"),
            playlist=[item],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            source_kind="browse",
            source_key="",
        ),
        current_index=0,
    )

    window._report_player_item_following_progress(item, position_seconds=19, duration_seconds=100)
    window._report_player_item_following_progress(item, position_seconds=20, duration_seconds=100)

    assert following.progress_calls == [(7, 24, 20)]


def test_main_window_reports_following_playback_source_after_threshold(qtbot) -> None:
    class SourceTrackingFollowingController(FakeFollowingController):
        def __init__(self) -> None:
            super().__init__()
            self.source_calls: list[dict[str, object]] = []

        def load_page(self, *, page: int, size: int, keyword: str, only_updates: bool):
            return [
                SimpleNamespace(
                    record=FollowingRecord(
                        id=7,
                        title="凡人修仙传",
                        provider="player",
                        provider_id="telegram::tg-vod-1",
                        current_season_number=1,
                        current_episode=20,
                        season_number=1,
                    )
                )
            ], 1

        def record_playback_progress(self, following_id: int, **kwargs) -> None:
            del following_id, kwargs

        def record_playback_source(self, following_id: int, **kwargs) -> None:
            self.source_calls.append({"following_id": following_id, **kwargs})

    following = SourceTrackingFollowingController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
    )
    qtbot.addWidget(window)

    playlist = [PlayItem(title=f"第{i}集", url=f"https://media.example/{i}.m3u8", vod_id="tg-vod-1") for i in range(1, 25)]
    item = playlist[19]
    window.player_window = SimpleNamespace(
        session=PlayerSession(
            vod=VodItem(vod_id="tg-vod-1", vod_name="凡人修仙传"),
            playlist=playlist,
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            source_kind="telegram",
            source_key="",
        ),
        current_index=19,
    )

    window._report_player_item_following_progress(item, position_seconds=20, duration_seconds=100)

    assert following.source_calls == [
        {
            "following_id": 7,
            "source_kind": "telegram",
            "source_key": "",
            "vod_id": "tg-vod-1",
            "current_season_number": 1,
            "current_episode": 20,
            "playlist_latest_episode": 24,
        }
    ]


def test_main_window_loads_following_when_tab_is_selected(qtbot) -> None:
    following = FakeFollowingController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
    )
    qtbot.addWidget(window)

    window.following_button.click()

    assert following.load_calls == [(1, 20, "", False)]


def test_main_window_switches_to_following_detail_before_loading_record(qtbot) -> None:
    window_ref: dict[str, MainWindow] = {}

    class OrderTrackingFollowingController(FakeFollowingController):
        def __init__(self) -> None:
            super().__init__()
            self.detail_load_saw_detail_page: list[bool] = []

        def load_detail(self, following_id: int, *, refresh_if_empty: bool = True):
            del refresh_if_empty
            window = window_ref["window"]
            self.detail_load_saw_detail_page.append(
                window.nav_tabs.currentWidget() is window.following_detail_page
            )
            return super().load_detail(following_id)

    following = OrderTrackingFollowingController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
    )
    window_ref["window"] = window
    qtbot.addWidget(window)

    window.nav_tabs.setCurrentWidget(window.following_page)
    window.open_following_detail(1)

    assert window.nav_tabs.currentWidget() is window.following_detail_page
    assert following.detail_load_saw_detail_page == []
    qtbot.waitUntil(lambda: following.detail_load_saw_detail_page == [True], timeout=1000)
    assert following.detail_load_saw_detail_page == [True]


def test_main_window_routes_following_bound_telegram_source(qtbot, monkeypatch) -> None:
    class BoundFollowingController(FakeFollowingController):
        def load_detail(self, following_id: int):
            return FollowingDetailView(
                record=FollowingRecord(
                    id=following_id,
                    title="凡人修仙传",
                    source_bindings=[FollowingSourceBinding(source_kind="telegram", source_key="", vod_id="tg-vod-1")],
                ),
                snapshot=FollowingDetailSnapshot(following_id=following_id),
            )

    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=BoundFollowingController(),
    )
    qtbot.addWidget(window)
    opened: list[OpenPlayerRequest] = []
    window.telegram_controller = SimpleNamespace(
        build_request=lambda vod_id: OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="凡人修仙传"),
            playlist=[PlayItem(title="第20集", url="https://media.example/20.m3u8")],
            clicked_index=0,
            source_kind="telegram",
            source_key="",
            playback_history_loader=lambda: HistoryRecord(
                id=0,
                key=vod_id,
                vod_name="凡人修仙传",
                vod_pic="",
                vod_remarks="第20集",
                episode=19,
                episode_url="https://media.example/20.m3u8",
                position=120000,
                opening=0,
                ending=0,
                speed=1.0,
                create_time=1,
                source_kind="telegram",
            ),
        )
    )
    monkeypatch.setattr(window, "_start_open_request", lambda builder: opened.append(builder()))

    window.open_following_bound_source(1)

    assert opened
    assert opened[0].vod.vod_id == "tg-vod-1"
    assert opened[0].playback_history_loader is not None


def test_main_window_shows_error_when_following_has_no_bound_source(qtbot, monkeypatch) -> None:
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=FakeFollowingController(),
    )
    qtbot.addWidget(window)
    errors: list[str] = []
    monkeypatch.setattr(window, "show_error", errors.append)

    window.open_following_bound_source(1)

    assert errors == ["暂无已绑定播放源，请先搜索播放"]


def test_main_window_homepage_prompt_actions(qtbot) -> None:
    following = FakeFollowingController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
    )
    qtbot.addWidget(window)

    window.show_following_homepage_prompts()

    assert window._following_prompt_dialog is not None
    window._following_prompt_detail_button.click()
    assert following.cleared == [1]


def test_main_window_homepage_prompt_dismiss_until_next_episode(qtbot) -> None:
    following = FakeFollowingController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
    )
    qtbot.addWidget(window)

    window.show_following_homepage_prompts()

    assert window._following_prompt_dialog is not None
    window._following_prompt_dismiss_until_next_button.click()
    assert following.dismissed_until_next == [1]
    assert window._following_prompt_dialog is None


def test_main_window_does_not_show_homepage_prompt_while_player_is_playing(qtbot) -> None:
    following = FakeFollowingController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
    )
    qtbot.addWidget(window)
    window.player_window = SimpleNamespace(is_playing=True, session=object())

    window.show_following_homepage_prompts()

    assert window._following_prompt_dialog is None
    assert following.cleared == []
    assert following.snoozed == []


def test_main_window_does_not_show_homepage_prompt_while_player_window_is_visible(qtbot) -> None:
    following = FakeFollowingController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
    )
    qtbot.addWidget(window)
    window.player_window = SimpleNamespace(
        is_playing=False,
        session=object(),
        isVisible=lambda: True,
    )

    window.show_following_homepage_prompts()

    assert window._following_prompt_dialog is None
    assert following.cleared == []
    assert following.snoozed == []


def test_main_window_closes_visible_homepage_prompt_when_opening_player(qtbot, monkeypatch) -> None:
    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            self.session = None
            self.opened: list[tuple[object, bool]] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.session = session
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    following = FakeFollowingController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
    )
    qtbot.addWidget(window)
    window.show_following_homepage_prompts()
    assert window._following_prompt_dialog is not None

    request = OpenPlayerRequest(
        vod=VodItem(vod_id="vod-1", vod_name="片名"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
        clicked_index=0,
    )
    session = window._create_player_session(request)
    window._apply_open_player(request, session)

    assert window._following_prompt_dialog is None
    assert following.cleared == []


def test_main_window_homepage_prompt_uses_tmdb_global_latest_count(qtbot) -> None:
    class LongRunningFollowingController(FakeFollowingController):
        def load_homepage_prompts(self):
            return [
                FollowingRecord(
                    id=1,
                    title="航海王",
                    provider="tmdb",
                    provider_id="tv:37854",
                    season_number=1,
                    current_season_number=15,
                    current_episode=581,
                    latest_episode=1163,
                    total_episodes=1181,
                    has_update=True,
                    new_episode_count=582,
                    homepage_prompt_pending=True,
                )
            ]

        def load_detail(self, following_id: int, *, refresh_if_empty: bool = True):
            del refresh_if_empty
            return FollowingDetailView(
                record=self.load_homepage_prompts()[0],
                snapshot=FollowingDetailSnapshot(
                    following_id=following_id,
                    seasons=[
                        FollowingSeason(season_number=0, episode_count=35, is_special=True),
                        FollowingSeason(season_number=15, episode_count=62),
                        FollowingSeason(season_number=23, episode_count=61),
                    ],
                    episodes=[
                        FollowingEpisode(season_number=23, episode_number=61, air_date="2026-05-24"),
                    ],
                    next_episode=FollowingEpisode(season_number=23, episode_number=1164, air_date="2026-05-31"),
                ),
            )

    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=LongRunningFollowingController(),
    )
    qtbot.addWidget(window)

    window.show_following_homepage_prompts()

    labels = [label.text() for label in window._following_prompt_dialog.findChildren(main_window_module.QLabel)]
    assert "更新 582 集，最新第 1163 集" in labels


def test_main_window_closing_homepage_prompt_dismisses_current_reminder(qtbot) -> None:
    following = FakeFollowingController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        following_controller=following,
    )
    qtbot.addWidget(window)

    window.show_following_homepage_prompts()
    assert window._following_prompt_dialog is not None

    window._following_prompt_dialog.close()

    assert following.cleared == [1]


def test_main_window_loads_favorites_when_tab_is_selected(qtbot) -> None:
    favorites = FakeFavoritesController()
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        favorites_controller=favorites,
    )
    qtbot.addWidget(window)

    window.favorites_button.click()

    assert favorites.load_calls == [(1, 20, "")]


def test_main_window_restored_favorites_tab_loads_once_while_startup_plugins_arrive(qtbot) -> None:
    favorites = FakeFavoritesController()

    def plugin_loader_task():
        yield {
            "id": "plugin-1",
            "title": "插件一",
            "controller": FakeSpiderController("插件一"),
            "search_enabled": False,
            "sort_order": 0,
        }
        yield {
            "id": "plugin-2",
            "title": "插件二",
            "controller": FakeSpiderController("插件二"),
            "search_enabled": False,
            "sort_order": 1,
        }

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(last_selected_tab="favorites"),
        favorites_controller=favorites,
        spider_plugins=[],
        plugin_loader_task=plugin_loader_task,
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)

    window.show()
    qtbot.waitUntil(lambda: window._startup_plugin_load_state == "idle")

    assert favorites.load_calls == [(1, 20, "")]


def test_main_window_favorites_tab_renders_new_context_menu_favorite(qtbot) -> None:
    class MemoryFavoritesController(FakeFavoritesController):
        def load_page(self, *, page: int, size: int, keyword: str):
            self.load_calls.append((page, size, keyword))
            items = []
            for payload in self.add_calls:
                record = FavoriteRecord(
                    source_kind=str(payload["source_kind"]),
                    source_key=str(payload["source_key"]),
                    source_name=str(payload["source_name"]),
                    vod_id=str(payload["vod_id"]),
                    vod_name_snapshot=str(payload["vod_name_snapshot"]),
                    latest_vod_name=str(payload["latest_vod_name"]),
                    vod_pic=str(payload["vod_pic"]),
                    vod_remarks=str(payload["vod_remarks"]),
                    title_changed=False,
                    created_at=int(payload["created_at"]),
                    updated_at=int(payload["updated_at"]),
                )
                items.append(
                    FavoriteCardItem(
                        record=record,
                        display_title=record.latest_vod_name,
                        source_label=record.source_name,
                        updated_hint=False,
                        secondary_text="",
                    )
                )
            return items, len(items)

    favorites = MemoryFavoritesController()
    window = MainWindow(
        telegram_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        favorites_controller=favorites,
    )
    qtbot.addWidget(window)

    window._handle_video_item_context_favorite(
        window.telegram_page,
        VodItem(vod_id="vod-1", vod_name="测试影片"),
    )
    window.favorites_button.click()

    assert [card.title_label.text() for card in window.favorites_page.card_widgets] == ["测试影片"]


def test_main_window_opens_browse_favorite_record(qtbot, monkeypatch) -> None:
    opened: list[OpenPlayerRequest] = []
    browse_controller = SimpleNamespace(
        build_request_from_detail=lambda vod_id: OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="详情页"),
            playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
            clicked_index=0,
            source_kind="browse",
            source_mode="detail",
            source_vod_id=vod_id,
        )
    )
    window = MainWindow(
        browse_controller=browse_controller,
        history_controller=DummyHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        favorites_controller=FakeFavoritesController(),
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_open_favorite_placeholder", lambda record: None)
    monkeypatch.setattr(window, "_start_open_request", lambda builder: opened.append(builder()) or 1)
    record = FavoriteRecord(
        source_kind="browse",
        source_key="",
        source_name="文件浏览",
        vod_id="detail-1",
        vod_name_snapshot="庆余年",
        latest_vod_name="庆余年",
        vod_pic="",
        vod_remarks="",
        title_changed=False,
        created_at=10,
        updated_at=10,
    )

    window.open_favorite_detail(record)

    assert opened[0].source_kind == "browse"


def test_main_window_opens_live_favorite_record(qtbot, monkeypatch) -> None:
    opened: list[OpenPlayerRequest] = []
    live_controller = SimpleNamespace(
        build_request=lambda vod_id: OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="直播详情"),
            playlist=[PlayItem(title="直播", url="https://media.example/live.m3u8")],
            clicked_index=0,
            source_kind="live",
            source_mode="detail",
            source_vod_id=vod_id,
        )
    )
    window = MainWindow(
        live_controller=live_controller,
        browse_controller=FakeStaticController(),
        history_controller=DummyHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        favorites_controller=FakeFavoritesController(),
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_open_favorite_placeholder", lambda record: None)
    monkeypatch.setattr(window, "_start_open_request", lambda builder: opened.append(builder()) or 1)

    window.open_favorite_detail(
        FavoriteRecord(
            source_kind="live",
            source_key="",
            source_name="网络直播",
            vod_id="live-1",
            vod_name_snapshot="直播频道",
            latest_vod_name="直播频道",
            vod_pic="",
            vod_remarks="",
            title_changed=False,
            created_at=10,
            updated_at=10,
        )
    )

    assert opened[0].source_kind == "live"


def test_main_window_favorite_click_opens_placeholder_player_immediately(qtbot, monkeypatch) -> None:
    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            self.session = None
            self.opened: list[tuple[object, bool]] = []
            self.logs: list[str] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.session = session
            self.opened.append((session, start_paused))
            message = session.get("initial_log_message", "")
            if message:
                self.logs.append(message)

        def append_status_log(self, message: str) -> None:
            self.logs.append(message)

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class SlowPluginController(FakeSpiderController):
        def __init__(self) -> None:
            super().__init__("插件一")
            self._event = threading.Event()

        def build_request(self, vod_id: str):
            self.open_calls.append(vod_id)
            assert self._event.wait(timeout=5), "favorite request was never released"
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="真实标题"),
                playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
                clicked_index=0,
                source_kind="plugin",
                source_key="plugin-1",
            )

        def release(self) -> None:
            self._event.set()

    controller = SlowPluginController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=DummyHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[{"id": "plugin-1", "title": "插件一", "controller": controller, "search_enabled": False}],
        favorites_controller=FakeFavoritesController(),
    )
    qtbot.addWidget(window)

    window.open_favorite_detail(
        FavoriteRecord(
            source_kind="plugin",
            source_key="plugin-1",
            source_name="插件一",
            vod_id="vod-1",
            vod_name_snapshot="收藏标题",
            latest_vod_name="收藏标题",
            vod_pic="poster.jpg",
            vod_remarks="完结",
            title_changed=False,
            created_at=10,
            updated_at=10,
        )
    )

    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1, timeout=1000)
    assert window.player_window.opened[0][0]["is_placeholder"] is True
    assert window.player_window.opened[0][0]["vod"].vod_name == "收藏标题"
    assert window.player_window.logs == ["正在加载详情..."]

    controller.release()
    qtbot.waitUntil(lambda: len(window.player_window.opened) == 2, timeout=1000)
    assert window.player_window.opened[1][0]["is_placeholder"] is False
    assert window.player_window.opened[1][0]["vod"].vod_name == "真实标题"


def test_main_window_enables_resize_support(qtbot) -> None:
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
    )
    qtbot.addWidget(window)

    assert window.is_window_resizable() is True


def test_main_window_global_search_button_styles_include_disabled_tokens(qtbot) -> None:
    from atv_player.ui.theme import ThemeManager, install_theme

    app = QApplication.instance() or QApplication([])
    manager = ThemeManager(system_theme_getter=lambda: "light")
    install_theme(app, manager, "light")

    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
    )
    qtbot.addWidget(window)

    tokens = manager.tokens_for("light")
    stylesheet = window.global_search_button.styleSheet()

    assert "QPushButton:disabled" in stylesheet
    assert f"background: {tokens.button_disabled_bg};" in stylesheet
    assert f"border: 1px solid {tokens.button_disabled_border};" in stylesheet
    assert f"color: {tokens.button_disabled_text};" in stylesheet


def test_main_window_global_search_icon_buttons_use_dark_theme_contrast_treatment(qtbot) -> None:
    from atv_player.ui.icon_cache import load_icon
    from atv_player.ui.theme import ThemeManager, install_theme

    app = QApplication.instance() or QApplication([])
    manager = ThemeManager(system_theme_getter=lambda: "dark")
    install_theme(app, manager, "dark")

    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        plugin_manager=FakePluginManager(),
    )
    qtbot.addWidget(window)
    window.show()

    tokens = manager.tokens_for("dark")
    button_qss = window.global_search_button.styleSheet()

    assert f"background: {tokens.button_bg};" in button_qss
    assert f"border: 1px solid {tokens.input_hover_border};" in button_qss
    assert f"border-color: {tokens.accent_hover};" in button_qss

    raw_search = load_icon(window._SEARCH_ICON_PATH).pixmap(24, 24).toImage()
    raw_hot = load_icon(window._SEARCH_POPUP_ICON_PATH).pixmap(24, 24).toImage()
    raw_heat = load_icon(window._HEAT_RECOMMENDATIONS_ICON_PATH).pixmap(24, 24).toImage()

    assert window.global_search_button.icon().pixmap(24, 24).toImage() != raw_search
    assert window.global_search_popup_button.icon().pixmap(24, 24).toImage() != raw_hot
    assert window.heat_recommendations_button.icon().pixmap(24, 24).toImage() != raw_heat
    assert window.global_search_button.width() == 36
    assert window.global_search_popup_button.width() == 36
    assert window.heat_recommendations_button.width() == 36
    assert window.heat_recommendations_button.toolTip() == "大家在看"


def test_main_window_navigation_tabs_use_explicit_dark_theme_text_colors(qtbot) -> None:
    from atv_player.ui.theme import ThemeManager, install_theme

    app = QApplication.instance() or QApplication([])
    manager = ThemeManager(system_theme_getter=lambda: "dark")
    install_theme(app, manager, "dark")

    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
    )
    qtbot.addWidget(window)

    tokens = manager.tokens_for("dark")
    stylesheet = window.nav_tabs.tab_bar.styleSheet()

    assert f"color: {tokens.text_primary};" in stylesheet
    assert f"color: {tokens.accent};" in stylesheet
    assert f"background: {tokens.panel_alt_bg};" in stylesheet


def test_main_window_navigation_tabs_do_not_use_scroll_buttons(qtbot) -> None:
    window = MainWindow(
        FakeStaticController(),
        DummyHistoryController(),
        FakePlayerController(),
        AppConfig(),
    )
    qtbot.addWidget(window)

    assert window.nav_tabs.tab_bar.usesScrollButtons() is False
    assert window.nav_tabs.tab_bar.cursor().shape() == Qt.CursorShape.PointingHandCursor


def test_global_search_popup_action_button_qss_uses_disabled_button_text_token() -> None:
    from atv_player.ui.theme import ThemeManager, current_tokens, install_theme

    app = QApplication.instance() or QApplication([])
    manager = ThemeManager(system_theme_getter=lambda: "dark")
    install_theme(app, manager, "dark")

    qss = main_window_module.GlobalSearchPopup._action_button_qss()
    tokens = current_tokens()

    assert "QPushButton:disabled" in qss
    assert f"color: {tokens.button_disabled_text};" in qss


class AsyncOpenController(FakeStaticController):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._event = threading.Event()

    def build_request(self, vod_id: str):
        self.calls.append(vod_id)
        assert self._event.wait(timeout=5), "open request was never released"
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="Movie"),
            playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )

    def release(self) -> None:
        self._event.set()


class AsyncMediaController(FakeStaticController):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._event = threading.Event()

    def load_folder_items(self, vod_id: str):
        self.calls.append(vod_id)
        assert self._event.wait(timeout=5), "media load was never released"
        return [VodItem(vod_id="m1", vod_name="Movie")], 1

    def release(self) -> None:
        self._event.set()


class AsyncRestoreController(FakeStaticController):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._event = threading.Event()

    def build_request_from_detail(self, vod_id: str):
        self.calls.append(vod_id)
        assert self._event.wait(timeout=5), "restore request was never released"
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="Movie"),
            playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )

    def release(self) -> None:
        self._event.set()


class SearchableController(FakeStaticController):
    def __init__(self, items: list[VodItem], total: int | None = None) -> None:
        self.items = list(items)
        self.total = len(items) if total is None else total
        self.search_calls: list[tuple[str, int]] = []

    def search_items(self, keyword: str, page: int):
        self.search_calls.append((keyword, page))
        return list(self.items), self.total


class GlobalSearchHistoryController:
    def __init__(self, records: list[HistoryRecord]) -> None:
        self.records = list(records)
        self.load_calls: list[tuple[int, int, str]] = []

    def load_page(self, page: int, size: int, *, keyword: str = "", **_kwargs):
        self.load_calls.append((page, size, keyword))
        filtered = [
            record
            for record in self.records
            if not keyword or keyword.casefold() in record.vod_name.casefold()
        ]
        start = max(page - 1, 0) * size
        end = start + size
        return filtered[start:end], len(filtered)

    def delete_many(self, records: list[HistoryRecord]) -> None:
        del records

    def clear_page(self, records: list[HistoryRecord]) -> None:
        del records


class PagedSearchableController(FakeStaticController):
    def __init__(self, results_by_page: dict[int, tuple[list[VodItem], int]]) -> None:
        self.results_by_page = {
            page: (list(items), total) for page, (items, total) in results_by_page.items()
        }
        self.search_calls: list[tuple[str, int]] = []

    def search_items(self, keyword: str, page: int):
        self.search_calls.append((keyword, page))
        return self.results_by_page.get(page, ([], 0))


class PageInfoSearchableController(FakeStaticController):
    uses_page_count_for_pagination = True

    def __init__(self) -> None:
        self.search_calls: list[tuple[str, int]] = []

    def search_items(self, keyword: str, page: int):
        self.search_calls.append((keyword, page))
        return [_vod(f"Telegram Page {page}")], PageInfo(2, 61)


class VariablePageSizeSearchableController(FakeStaticController):
    uses_result_length_for_pagination = True

    def __init__(self, results_by_page: dict[int, tuple[list[VodItem], int]]) -> None:
        self.results_by_page = {page: (list(items), total) for page, (items, total) in results_by_page.items()}
        self.search_calls: list[tuple[str, int]] = []

    def search_items(self, keyword: str, page: int):
        self.search_calls.append((keyword, page))
        return self.results_by_page[page]


class SearchableCategoryController(SearchableController):
    def __init__(self, category_item: VodItem, search_items: list[VodItem], total: int | None = None) -> None:
        super().__init__(search_items, total=total)
        self.load_categories_calls = 0
        self.load_items_calls: list[tuple[str, int]] = []
        self.category_item = category_item

    def load_categories(self):
        self.load_categories_calls += 1
        return [type("Category", (), {"type_id": "movie", "type_name": "电影", "filters": []})()]

    def load_items(self, category_id: str, page: int):
        self.load_items_calls.append((category_id, page))
        return [self.category_item], 1


class KeywordSearchableController(FakeStaticController):
    def __init__(self, results_by_keyword: dict[str, tuple[list[VodItem], int]]) -> None:
        self.results_by_keyword = {
            keyword: (list(items), total) for keyword, (items, total) in results_by_keyword.items()
        }
        self.search_calls: list[tuple[str, int]] = []

    def search_items(self, keyword: str, page: int):
        self.search_calls.append((keyword, page))
        return self.results_by_keyword.get(keyword, ([], 0))


class AsyncKeywordSearchController(FakeStaticController):
    def __init__(self, results_by_keyword: dict[str, tuple[list[VodItem], int]]) -> None:
        self.results_by_keyword = {
            keyword: (list(items), total) for keyword, (items, total) in results_by_keyword.items()
        }
        self.search_calls: list[tuple[str, int]] = []
        self._events: dict[str, threading.Event] = {}

    def search_items(self, keyword: str, page: int):
        self.search_calls.append((keyword, page))
        event = self._events.setdefault(keyword, threading.Event())
        assert event.wait(timeout=5), f"search for {keyword} was never released"
        return self.results_by_keyword.get(keyword, ([], 0))

    def release(self, keyword: str) -> None:
        self._events.setdefault(keyword, threading.Event()).set()


class SearchableResolveController(SearchableController):
    def __init__(self, items: list[VodItem], resolved_path: str, total: int | None = None) -> None:
        super().__init__(items, total=total)
        self.resolved_path = resolved_path
        self.resolve_calls: list[str] = []

    def resolve_search_result(self, item: VodItem) -> str:
        self.resolve_calls.append(item.vod_id)
        return self.resolved_path


class SmartSearchGlobalController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search_items(self, keyword: str, page: int):
        self.calls.append((keyword, page))
        return [VodItem(vod_id="smart-1", vod_name="黑镜", vod_remarks="科幻匹配")], 1


def _vod(name: str, vod_id: str | None = None, remarks: str = "") -> VodItem:
    return VodItem(vod_id=vod_id or name, vod_name=name, vod_pic="", vod_remarks=remarks)


def _history_record(name: str, key: str = "history-1", source_kind: str = "telegram") -> HistoryRecord:
    return HistoryRecord(
        id=0,
        key=key,
        vod_name=name,
        vod_pic="",
        vod_remarks="第2集",
        episode=1,
        episode_url="2.m3u8",
        position=90000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1234567890000,
        source_kind=source_kind,
        source_name="电报影视" if source_kind == "telegram" else "",
    )


def _popup_history_texts(window: MainWindow) -> list[str]:
    return window._global_search_popup.history_item_texts()


def _popup_delete_button(window: MainWindow, keyword: str):
    return window._global_search_popup.history_delete_button(keyword)


def _popup_hot_texts(window: MainWindow) -> list[str]:
    return window._global_search_popup.hot_item_texts()


def _popup_hot_tab_titles(window: MainWindow) -> list[str]:
    return window._global_search_popup.hot_tab_titles()


def _popup_hot_source_titles(window: MainWindow) -> list[str]:
    return window._global_search_popup.hot_source_titles()


def _popup_history_row(window: MainWindow, keyword: str):
    return window._global_search_popup.history_item_row(keyword)


def _popup_hot_ranks(window: MainWindow) -> list[str]:
    return window._global_search_popup.hot_item_ranks()


def test_main_window_keeps_personal_tabs_before_dynamic_spider_tabs(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"title": "红果短剧", "controller": FakeSpiderController("红果短剧"), "search_enabled": True},
            {"title": "短剧二号", "controller": FakeSpiderController("短剧二号"), "search_enabled": False},
        ],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.resize(920, 520)
    window.show()

    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == [
        "豆瓣电影",
        "环球片单",
        "电报影视",
        "网络直播",
        "Emby",
        "Jellyfin",
        "飞牛影视",
        "文件浏览",
        "我的收藏",
        "我的追更",
        "播放记录",
        "红果短剧",
        "短剧二号",
    ]


def test_main_window_places_global_catalog_after_douban(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        global_catalog_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)

    assert [window.nav_tabs.tabText(i) for i in range(3)] == ["豆瓣电影", "环球片单", "电报影视"]
    assert window._builtin_tab_definitions[1].key == "global_catalog"


def test_main_window_keeps_backend_telegram_and_adds_local_channel_tab(qtbot) -> None:
    backend_telegram = FakeStaticController()
    local_telegram = SearchableController([_vod("本地频道")], total=1)
    window = MainWindow(
        douban_controller=FakeStaticController(),
        global_catalog_controller=FakeStaticController(),
        telegram_controller=backend_telegram,
        telegram_channels_controller=local_telegram,
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)

    builtin_by_key = {definition.key: definition for definition in window._builtin_tab_definitions}

    assert builtin_by_key["telegram"].title == "电报影视"
    assert builtin_by_key["telegram"].search_controller is backend_telegram
    assert builtin_by_key["telegram_channels"].title == "电报频道"
    assert builtin_by_key["telegram_channels"].search_controller is local_telegram


class FakeMediaDetailController:
    def __init__(self) -> None:
        self.vod_calls: list[VodItem] = []
        self.heat_calls: list[object] = []
        self.identity_calls: list[MediaDetailIdentity] = []
        self.lookup_calls: list[MediaDetailLookup] = []
        self.refresh_calls: list[MediaDetailView] = []
        self.season_calls: list[tuple[MediaDetailView, int]] = []
        self.following_candidates: list[MediaDetailView] = []

    def placeholder_from_vod(self, vod: VodItem) -> MediaDetailView:
        return MediaDetailView(
            identity=MediaDetailIdentity(media_type="tv", tmdb_id="1399", title=vod.vod_name or "权力的游戏"),
            title=vod.vod_name or "权力的游戏",
            media_type="tv",
            year="",
            overview="正在加载元数据...",
        )

    def placeholder_from_heat(self, item: object) -> MediaDetailView:
        return MediaDetailView(
            identity=MediaDetailIdentity(media_type="tv", tmdb_id="1399", title=str(getattr(item, "title", "") or "权力的游戏")),
            title=str(getattr(item, "title", "") or "权力的游戏"),
            media_type="tv",
            year=str(getattr(item, "year", "") or ""),
            poster_url=str(getattr(item, "poster", "") or ""),
            overview="正在加载元数据...",
        )

    def placeholder_from_identity(self, identity: MediaDetailIdentity) -> MediaDetailView:
        return MediaDetailView(
            identity=identity,
            title=identity.title,
            media_type=identity.media_type,
            overview="正在加载元数据...",
        )

    def placeholder_from_lookup(self, lookup: MediaDetailLookup) -> MediaDetailView:
        return MediaDetailView(
            identity=MediaDetailIdentity(media_type=lookup.media_type or "tv", tmdb_id="", title=lookup.title),
            title=lookup.title,
            media_type=lookup.media_type or "tv",
            year=lookup.year,
            poster_url=lookup.poster_url,
            overview="正在加载元数据...",
        )

    def load_from_vod(self, vod: VodItem) -> MediaDetailView:
        self.vod_calls.append(vod)
        return _media_detail_view(
            MediaDetailIdentity(media_type="tv", tmdb_id="1399", title=vod.vod_name or "权力的游戏")
        )

    def load_from_heat(self, item: object) -> MediaDetailView:
        self.heat_calls.append(item)
        return _media_detail_view(MediaDetailIdentity(media_type="tv", tmdb_id="1399", title="权力的游戏"))

    def load_from_identity(self, identity: MediaDetailIdentity) -> MediaDetailView:
        self.identity_calls.append(identity)
        return _media_detail_view(identity)

    def load_from_lookup(self, lookup: MediaDetailLookup) -> MediaDetailView:
        self.lookup_calls.append(lookup)
        return _media_detail_view(MediaDetailIdentity(media_type=lookup.media_type or "tv", tmdb_id="100", title=lookup.title))

    def refresh(self, view: MediaDetailView) -> MediaDetailView:
        time.sleep(0.02)
        self.refresh_calls.append(view)
        return _media_detail_view(view.identity, overview="刷新后的简介")

    def load_season(self, view: MediaDetailView, *, season_number: int) -> MediaDetailView:
        self.season_calls.append((view, season_number))
        return _media_detail_view(
            view.identity,
            episodes=[
                MediaDetailEpisode(
                    season_number=season_number,
                    episode_number=1,
                    title=f"第 {season_number} 季第 1 集",
                )
            ],
        )

    def candidate_for_following(self, view: MediaDetailView):
        self.following_candidates.append(view)
        return SimpleNamespace(provider="tmdb", provider_id=f"{view.media_type}:{view.identity.tmdb_id}", title=view.title)

    def search_title(self, view: MediaDetailView) -> str:
        return view.title


def _media_detail_view(
    identity: MediaDetailIdentity,
    *,
    overview: str = "九大家族争夺铁王座。",
    episodes: list[MediaDetailEpisode] | None = None,
) -> MediaDetailView:
    return MediaDetailView(
        identity=identity,
        title=identity.title or "权力的游戏",
        media_type=identity.media_type,
        year="2011",
        release_date="2011-04-17",
        overview=overview,
        rating="8.4",
        genres=["剧情"],
        seasons=[
            {"season_number": 1, "name": "第 1 季", "episode_count": 1},
            {"season_number": 2, "name": "第 2 季", "episode_count": 1},
        ],
        episodes=episodes or [MediaDetailEpisode(season_number=1, episode_number=1, title="凛冬将至")],
        people=[MediaDetailPerson(name="Emilia Clarke", role="Daenerys Targaryen")],
        related=[
            MediaDetailRecommendation(
                identity=MediaDetailIdentity(media_type="tv", tmdb_id="1412", title="绿箭侠"),
                year="2012",
            )
        ],
    )


def test_global_catalog_card_click_opens_universal_media_detail(qtbot) -> None:
    detail_controller = FakeMediaDetailController()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        global_catalog_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        media_detail_controller=detail_controller,
    )
    qtbot.addWidget(window)

    item = VodItem(vod_id="tmdb:tv:1399", vod_name="权力的游戏")
    window.global_catalog_page.item_open_requested.emit(item)

    assert window.nav_tabs.currentWidget() is window.media_detail_page
    assert window.media_detail_page.title_label.text() == "权力的游戏"
    assert window.media_detail_page.status_label.text() == "正在加载元数据..."
    assert window.media_detail_page.episode_browser.episode_model.rowCount() == 0
    qtbot.waitUntil(lambda: detail_controller.vod_calls == [item], timeout=1000)
    qtbot.waitUntil(lambda: window.media_detail_page.episode_browser.episode_model.rowCount() == 1, timeout=1000)


def test_heat_recommendation_click_opens_universal_media_detail(qtbot) -> None:
    detail_controller = FakeMediaDetailController()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        global_catalog_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        media_detail_controller=detail_controller,
    )
    qtbot.addWidget(window)

    heat_item = SimpleNamespace(media_key="tmdb:tv:1399", title="权力的游戏", media_type="tv")
    window._handle_global_search_popup_heat_item_clicked(heat_item)

    assert window.nav_tabs.currentWidget() is window.media_detail_page
    assert window.media_detail_page.title_label.text() == "权力的游戏"
    assert window.media_detail_page.status_label.text() == "正在加载元数据..."
    qtbot.waitUntil(lambda: detail_controller.heat_calls == [heat_item], timeout=1000)
    qtbot.waitUntil(lambda: len(window.media_detail_page.person_cards) > 0, timeout=1000)
    assert window.media_detail_page.person_cards[0].name_label.text() == "Emilia Clarke"
    assert window.media_detail_page.person_cards[0].role_label.text() == "Daenerys Targaryen"


def test_media_detail_actions_search_follow_refresh_and_related(qtbot, monkeypatch) -> None:
    detail_controller = FakeMediaDetailController()
    following = FakeFollowingController()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        global_catalog_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        following_controller=following,
        config=AppConfig(),
        media_detail_controller=detail_controller,
    )
    qtbot.addWidget(window)
    searches: list[bool] = []
    monkeypatch.setattr(window, "_start_global_search", lambda **kwargs: searches.append(bool(kwargs)))

    view = _media_detail_view(MediaDetailIdentity(media_type="tv", tmdb_id="1399", title="权力的游戏"))
    window.media_detail_page.load_view(view)

    window.media_detail_page.search_play_requested.emit(view)
    assert window.global_search_edit.text() == "权力的游戏"
    assert searches == [True]

    window.media_detail_page.add_following_requested.emit(view)
    assert following.added_candidates[0].title == "权力的游戏"

    window.media_detail_page.refresh_metadata_requested.emit(view)
    assert window.media_detail_page.status_label.text() == "正在更新元数据..."
    assert window.media_detail_page.refresh_metadata_button.isEnabled() is False
    qtbot.waitUntil(lambda: detail_controller.refresh_calls == [view], timeout=1000)
    qtbot.waitUntil(lambda: "刷新后的简介" in window.media_detail_page.overview_label.text(), timeout=1000)
    assert window.media_detail_page.refresh_metadata_button.isEnabled() is True
    assert "刷新后的简介" in window.media_detail_page.overview_label.text()

    related_identity = MediaDetailIdentity(media_type="tv", tmdb_id="1412", title="绿箭侠")
    window.media_detail_page.related_open_requested.emit(related_identity)
    assert detail_controller.identity_calls == [related_identity]
    assert window.media_detail_page.title_label.text() == "绿箭侠"


def test_media_detail_season_switch_loads_selected_season(qtbot) -> None:
    detail_controller = FakeMediaDetailController()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        global_catalog_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        media_detail_controller=detail_controller,
    )
    qtbot.addWidget(window)
    view = _media_detail_view(MediaDetailIdentity(media_type="tv", tmdb_id="1399", title="权力的游戏"))
    window.media_detail_page.load_view(view)

    window.media_detail_page.season_requested.emit(view, 2)

    qtbot.waitUntil(lambda: detail_controller.season_calls == [(view, 2)], timeout=1000)
    qtbot.waitUntil(
        lambda: window.media_detail_page.episode_browser.episode_model.index(0, 0).data(EPISODE_ROLE).title
        == "第 2 季第 1 集",
        timeout=1000,
    )


def test_main_window_applies_builtin_tab_overrides_but_keeps_header_shortcuts(qtbot) -> None:
    config = AppConfig(
        builtin_tab_overrides_json=(
            '{"order":["telegram","douban","live","browse","favorites","following","history"],'
            '"hidden":["history"],"renames":{"douban":"电影"}}'
        )
    )
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.resize(920, 520)
    window.show()

    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == [
        "电报影视",
        "电影",
        "网络直播",
        "文件浏览",
        "我的收藏",
        "我的追更",
        "环球片单",
        "Emby",
        "Jellyfin",
        "飞牛影视",
    ]

    window.history_button.click()

    assert window.nav_tabs.currentWidget() is window.history_page
    assert "播放记录" not in [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())]


def test_main_window_refreshes_builtin_tabs_after_saving_overrides(qtbot) -> None:
    config = AppConfig()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    window._handle_builtin_tab_overrides_saved(
        '{"order":["history","douban"],"hidden":["history"],"renames":{"douban":"电影"}}'
    )

    assert config.builtin_tab_overrides_json == '{"order":["history","douban"],"hidden":["history"],"renames":{"douban":"电影"}}'
    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())][:3] == ["电影", "环球片单", "电报影视"]


def test_main_window_builtin_tab_context_menu_renames_tab(qtbot, monkeypatch) -> None:
    config = AppConfig()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        plugin_manager=FakePluginManager(),
    )
    qtbot.addWidget(window)
    window.show()

    class FakeMenu:
        rename_action = object()

        def __init__(self, parent=None) -> None:
            del parent

        def addAction(self, text: str):
            return self.rename_action if text == "重命名" else object()

        def exec(self, global_pos):
            del global_pos
            return self.rename_action

    monkeypatch.setattr(main_window_module, "QMenu", FakeMenu)
    monkeypatch.setattr(main_window_module.QInputDialog, "getText", lambda *args, **kwargs: ("电影", True))

    window._open_builtin_tab_context_menu("douban", window.mapToGlobal(window.rect().center()))

    assert config.builtin_tab_overrides_json == '{"order":["douban","global_catalog","telegram","live","emby","jellyfin","feiniu","browse","favorites","following","history"],"renames":{"douban":"电影"}}'
    assert window.nav_tabs.tabText(0) == "电影"


def test_main_window_builtin_tab_context_menu_hides_tab(qtbot, monkeypatch) -> None:
    config = AppConfig()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        plugin_manager=FakePluginManager(),
    )
    qtbot.addWidget(window)
    window.show()

    class FakeMenu:
        hide_action = object()

        def __init__(self, parent=None) -> None:
            del parent

        def addAction(self, text: str):
            return self.hide_action if text == "隐藏" else object()

        def exec(self, global_pos):
            del global_pos
            return self.hide_action

    monkeypatch.setattr(main_window_module, "QMenu", FakeMenu)

    window._open_builtin_tab_context_menu("history", window.mapToGlobal(window.rect().center()))

    assert '"hidden":["history"]' in config.builtin_tab_overrides_json
    assert "播放记录" not in [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())]
    window.history_button.click()
    assert window.nav_tabs.currentWidget() is window.history_page


def test_main_window_clears_visible_tab_highlight_when_opening_hidden_builtin_from_header_button(qtbot) -> None:
    config = AppConfig(
        builtin_tab_overrides_json='{"order":["douban","telegram","live","browse","favorites","following","history"],"hidden":["history"]}'
    )
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        plugin_manager=FakePluginManager(),
    )
    qtbot.addWidget(window)
    window.show()

    window.history_button.click()

    assert config.last_selected_tab == "history"
    assert window.nav_tabs.currentWidget() is window.history_page
    assert window.nav_tabs.tab_bar.property("hiddenTabActive") is True


def test_main_window_restores_hidden_builtin_tab_after_restart_without_visible_highlight(qtbot) -> None:
    config = AppConfig(
        builtin_tab_overrides_json='{"order":["douban","telegram","live","browse","favorites","following","history"],"hidden":["history"]}',
        last_selected_tab="history",
    )
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        plugin_manager=FakePluginManager(),
    )
    qtbot.addWidget(window)
    window.show()

    assert "播放记录" not in [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())]
    assert window.nav_tabs.currentWidget() is window.history_page
    assert window.nav_tabs.tab_bar.property("hiddenTabActive") is True


def test_main_window_header_management_actions_use_icon_buttons_with_tooltips(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        live_source_manager=object(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    buttons = [
        window.browse_button,
        window.favorites_button,
        window.following_button,
        window.history_button,
        window.plugin_manager_button,
        window.live_source_manager_button,
        window.advanced_settings_button,
        window.logout_button,
    ]

    assert [button.text() for button in buttons] == ["", "", "", "", "", "", "", ""]
    assert [button.toolTip() for button in buttons] == [
        "文件浏览",
        "我的收藏",
        "我的追更",
        "播放记录",
        "插件管理",
        "直播源管理",
        "高级设置",
        "退出登录",
    ]
    assert all(not button.icon().isNull() for button in buttons)
    assert all(button.width() == 36 for button in buttons)
    assert [
        window.header_layout.indexOf(button)
        for button in buttons
    ] == sorted(window.header_layout.indexOf(button) for button in buttons)
    assert len(window.header_action_separators) == 2
    assert [
        window.header_layout.indexOf(window.history_button),
        window.header_layout.indexOf(window.header_action_separators[0]),
        window.header_layout.indexOf(window.plugin_manager_button),
        window.header_layout.indexOf(window.live_source_manager_button),
        window.header_layout.indexOf(window.advanced_settings_button),
        window.header_layout.indexOf(window.header_action_separators[1]),
        window.header_layout.indexOf(window.logout_button),
    ] == sorted(
        [
            window.header_layout.indexOf(window.history_button),
            window.header_layout.indexOf(window.header_action_separators[0]),
            window.header_layout.indexOf(window.plugin_manager_button),
            window.header_layout.indexOf(window.live_source_manager_button),
            window.header_layout.indexOf(window.advanced_settings_button),
            window.header_layout.indexOf(window.header_action_separators[1]),
            window.header_layout.indexOf(window.logout_button),
        ]
    )

    window.history_button.click()
    assert window.nav_tabs.currentWidget() is window.history_page
    window.browse_button.click()
    assert window.nav_tabs.currentWidget() is window.browse_page


def test_main_window_hides_pansou_tab_until_global_search_has_results(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        feiniu_controller=FakeStaticController(),
        pansou_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.resize(920, 520)
    window.show()

    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == [
        "豆瓣电影",
        "环球片单",
        "电报影视",
        "网络直播",
        "Emby",
        "Jellyfin",
        "飞牛影视",
        "文件浏览",
        "我的收藏",
        "我的追更",
        "播放记录",
    ]


def test_main_window_shows_startup_plugin_loading_placeholder_tab(qtbot) -> None:
    load_started = threading.Event()
    release_load = threading.Event()

    def plugin_loader_task():
        load_started.set()
        assert release_load.wait(timeout=5), "plugin load was never released"
        return []

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[],
        plugin_loader_task=plugin_loader_task,
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    assert load_started.wait(timeout=1)
    assert "插件加载中" in [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())]

    release_load.set()


def test_main_window_does_not_precreate_global_search_popup_during_startup_plugin_load(qtbot) -> None:
    load_started = threading.Event()
    release_load = threading.Event()

    def plugin_loader_task():
        load_started.set()
        assert release_load.wait(timeout=5), "plugin load was never released"
        return []

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[],
        plugin_loader_task=plugin_loader_task,
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    assert load_started.wait(timeout=1)
    QApplication.processEvents()

    top_level_popups = [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, main_window_module.GlobalSearchPopup)
    ]
    assert top_level_popups == []

    release_load.set()


def test_main_window_creates_startup_plugin_pages_under_navigation_stack(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)

    page, _controller, _plugin_id, _tab_definition = window._create_plugin_page_entry(
        {"id": "plugin-1", "title": "红果短剧", "controller": FakeSpiderController("红果短剧"), "search_enabled": True}
    )

    assert page.parent() is window.nav_tabs.content_stack
    assert page.window() is window


def test_startup_plugin_page_construction_does_not_show_unparented_child_widgets(qtbot) -> None:
    unparented_shows: list[str] = []

    class TopLevelShowRecorder(QObject):
        def eventFilter(self, watched, event) -> bool:
            if (
                event.type() == QEvent.Type.Show
                and isinstance(watched, QWidget)
                and watched.parent() is None
                and watched.isWindow()
            ):
                class_name = type(watched).__name__
                if class_name != "MainWindow":
                    unparented_shows.append(class_name)
            return False

    app = QApplication.instance()
    recorder = TopLevelShowRecorder()
    app.installEventFilter(recorder)
    try:
        window = MainWindow(
            douban_controller=FakeStaticController(),
            telegram_controller=FakeStaticController(),
            live_controller=FakeStaticController(),
            emby_controller=FakeStaticController(),
            jellyfin_controller=FakeStaticController(),
            browse_controller=FakeStaticController(),
            history_controller=FakeStaticController(),
            player_controller=FakePlayerController(),
            config=AppConfig(),
            spider_plugins=[],
            plugin_manager=WidthAwarePluginManager(),
        )
        qtbot.addWidget(window)
        window._create_plugin_page_entry(
            {
                "id": "plugin-1",
                "title": "红果短剧",
                "controller": FakeSpiderController("红果短剧"),
                "search_enabled": True,
            }
        )
        QApplication.processEvents()
    finally:
        app.removeEventFilter(recorder)

    assert unparented_shows == []


def test_main_window_replaces_loading_placeholder_with_loaded_plugin_tabs(qtbot) -> None:
    release_load = threading.Event()

    def plugin_loader_task():
        assert release_load.wait(timeout=5), "plugin load was never released"
        return [
            {"id": "plugin-1", "title": "红果短剧", "controller": FakeSpiderController("红果短剧"), "search_enabled": True},
            {"id": "plugin-2", "title": "短剧二号", "controller": FakeSpiderController("短剧二号"), "search_enabled": False},
        ]

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[],
        plugin_loader_task=plugin_loader_task,
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    window.resize(920, 520)
    window.show()

    release_load.set()

    qtbot.waitUntil(
        lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == [
            "豆瓣电影",
            "环球片单",
            "电报影视",
            "网络直播",
            "Emby",
            "Jellyfin",
            "飞牛影视",
            "文件浏览",
            "我的收藏",
            "我的追更",
            "播放记录",
            "红果短剧",
            "短剧二号",
        ]
    )


def test_main_window_shows_incrementally_loaded_plugin_tabs_before_startup_load_finishes(qtbot) -> None:
    release_load = threading.Event()

    def plugin_loader_task():
        yield {
            "id": "plugin-2",
            "title": "短剧二号",
            "controller": FakeSpiderController("短剧二号"),
            "search_enabled": False,
            "sort_order": 0,
        }
        assert release_load.wait(timeout=5), "plugin load was never released"
        yield {
            "id": "plugin-1",
            "title": "红果短剧",
            "controller": FakeSpiderController("红果短剧"),
            "search_enabled": True,
            "sort_order": 1,
        }

    config = AppConfig(last_selected_tab="plugin:plugin-2")
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        spider_plugins=[],
        plugin_loader_task=plugin_loader_task,
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    window.resize(920, 520)
    window.show()

    qtbot.waitUntil(
        lambda: len(window._plugin_pages) == 1
        and [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == [
            "豆瓣电影",
            "环球片单",
            "电报影视",
            "网络直播",
            "Emby",
            "Jellyfin",
            "飞牛影视",
            "文件浏览",
            "我的收藏",
            "我的追更",
            "播放记录",
            "短剧二号",
        ]
    )
    assert window._startup_plugin_load_state == "loading"
    assert window.nav_tabs.currentWidget() is window._plugin_pages[0][0]

    release_load.set()

    qtbot.waitUntil(
        lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == [
            "豆瓣电影",
            "环球片单",
            "电报影视",
            "网络直播",
            "Emby",
            "Jellyfin",
            "飞牛影视",
            "文件浏览",
            "我的收藏",
            "我的追更",
            "播放记录",
            "短剧二号",
            "红果短剧",
        ]
    )


def test_main_window_restores_last_selected_plugin_tab_after_async_startup_load(qtbot) -> None:
    release_load = threading.Event()

    def plugin_loader_task():
        assert release_load.wait(timeout=5), "plugin load was never released"
        return [
            {"id": "plugin-1", "title": "红果短剧", "controller": FakeSpiderController("红果短剧"), "search_enabled": True},
            {"id": "plugin-2", "title": "短剧二号", "controller": FakeSpiderController("短剧二号"), "search_enabled": False},
        ]

    config = AppConfig(last_selected_tab="plugin:plugin-2")
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        spider_plugins=[],
        plugin_loader_task=plugin_loader_task,
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    window.resize(920, 520)
    window.show()

    assert config.last_selected_tab == "plugin:plugin-2"
    release_load.set()

    qtbot.waitUntil(
        lambda: len(window._plugin_pages) > 1 and window.nav_tabs.currentWidget() is window._plugin_pages[1][0]
    )
    assert config.last_selected_tab == "plugin:plugin-2"


def test_main_window_restores_saved_visible_plugin_tab_after_geometry_restore(qtbot) -> None:
    plugin_definitions = [
        {"id": "plugin-1", "title": "插件一", "controller": FakeSpiderController("插件一"), "search_enabled": True}
    ]
    source = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=plugin_definitions,
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(source)
    source.resize(920, 520)
    source.show()
    geometry = bytes(source.saveGeometry())
    source.close()

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(last_selected_tab="plugin:plugin-1", main_window_geometry=geometry),
        spider_plugins=plugin_definitions,
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    assert "插件一" in [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())]
    assert window.nav_tabs.currentWidget() is window._plugin_pages[0][0]


def test_main_window_restores_plugin_player_as_soon_as_target_plugin_arrives_during_startup_load(
    qtbot, monkeypatch,
) -> None:
    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            self.opened: list[tuple[object, bool]] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class RestorePluginController:
        def __init__(self, vod_name: str) -> None:
            self.vod_name = vod_name

        def load_categories(self):
            return []

        def load_items(self, category_id: str, page: int):
            return [], 0

        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name=self.vod_name),
                playlist=[PlayItem(title="第2集", url="https://media.example/2.m3u8")],
                clicked_index=0,
                source_kind="plugin",
                source_mode="detail",
                source_vod_id=vod_id,
            )

    release_load = threading.Event()

    def plugin_loader_task():
        yield {
            "id": "plugin-1",
            "title": "插件一",
            "controller": RestorePluginController("插件电影"),
            "search_enabled": False,
            "sort_order": 0,
        }
        assert release_load.wait(timeout=5), "plugin load was never released"
        yield {
            "id": "plugin-2",
            "title": "插件二",
            "controller": RestorePluginController("插件二电影"),
            "search_enabled": False,
            "sort_order": 1,
        }

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
        last_playback_source="plugin",
        last_playback_source_key="plugin-1",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
        last_player_paused=True,
    )
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        spider_plugins=[],
        plugin_loader_task=plugin_loader_task,
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    window._start_restore_last_player()

    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) >= 2)
    assert window._startup_plugin_load_state == "loading"
    assert window.player_window.opened[0][0]["is_placeholder"] is True
    assert window.player_window.opened[1][1] is True
    assert window.player_window.opened[1][0]["vod"].vod_name == "插件电影"

    release_load.set()
    qtbot.waitUntil(lambda: len(window._plugin_pages) == 2)
    assert len(window.player_window.opened) >= 2


def test_main_window_plugin_restore_opens_placeholder_player_while_restore_request_loads(
    qtbot, monkeypatch,
) -> None:
    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            self.session = None
            self.opened: list[tuple[object, bool]] = []
            self.logs: list[str] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.session = session
            self.opened.append((session, start_paused))
            message = session.get("initial_log_message", "")
            if message:
                self.logs.append(message)

        def append_status_log(self, message: str) -> None:
            self.logs.append(message)

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class SlowRestorePluginController:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self._event = threading.Event()

        def load_categories(self):
            return []

        def load_items(self, category_id: str, page: int):
            return [], 0

        def build_request(self, vod_id: str):
            self.calls.append(vod_id)
            assert self._event.wait(timeout=5), "restore request was never released"
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="插件电影"),
                playlist=[PlayItem(title="第2集", url="https://media.example/2.m3u8")],
                clicked_index=0,
                source_kind="plugin",
                source_mode="detail",
                source_vod_id=vod_id,
            )

        def release(self) -> None:
            self._event.set()

    controller = SlowRestorePluginController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
        last_playback_source="plugin",
        last_playback_source_key="plugin-1",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
        last_player_paused=True,
    )
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        spider_plugins=[{"id": "plugin-1", "title": "插件一", "controller": controller, "search_enabled": False}],
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    window._start_restore_last_player()

    qtbot.waitUntil(
        lambda: (
            window.player_window is not None
            and len(window.player_window.opened) == 1
            and window.isHidden() is True
        )
    )
    assert window.isHidden() is True
    assert window.player_window.opened[0][0]["is_placeholder"] is True
    assert window.player_window.logs == ["正在恢复播放..."]
    assert controller.calls == ["vod-1"]

    controller.release()

    qtbot.waitUntil(lambda: len(window.player_window.opened) == 2)
    assert window.player_window.opened[1][0]["is_placeholder"] is False
    assert window.player_window.opened[1][0]["vod"].vod_name == "插件电影"
    assert window.player_window.opened[1][1] is True


def test_main_window_defers_plugin_player_restore_until_async_startup_load_finishes(qtbot, monkeypatch) -> None:
    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            self.opened: list[tuple[object, bool]] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class RestorePluginController:
        def load_categories(self):
            return []

        def load_items(self, category_id: str, page: int):
            return [], 0

        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="插件电影"),
                playlist=[PlayItem(title="第2集", url="https://media.example/2.m3u8")],
                clicked_index=0,
                source_kind="plugin",
                source_mode="detail",
                source_vod_id=vod_id,
            )

    release_load = threading.Event()

    def plugin_loader_task():
        assert release_load.wait(timeout=5), "plugin load was never released"
        return [
            {"id": "plugin-1", "title": "插件一", "controller": RestorePluginController(), "search_enabled": False},
        ]

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
        last_playback_source="plugin",
        last_playback_source_key="plugin-1",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
        last_player_paused=True,
    )
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        spider_plugins=[],
        plugin_loader_task=plugin_loader_task,
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    window._start_restore_last_player()

    assert window.player_window is None
    release_load.set()

    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) >= 2)
    assert window.player_window.opened[0][0]["is_placeholder"] is True
    assert window.player_window.opened[1][1] is True


def test_main_window_shows_retry_after_startup_plugin_load_failure(qtbot) -> None:
    attempts = {"count": 0}

    def plugin_loader_task():
        attempts["count"] += 1
        raise RuntimeError("boom")

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[],
        plugin_loader_task=plugin_loader_task,
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    qtbot.waitUntil(lambda: window.startup_plugin_retry_button.isVisible())
    assert "插件加载失败" in [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())]


def test_main_window_retry_restarts_startup_plugin_loading(qtbot) -> None:
    attempts = {"count": 0}

    def plugin_loader_task():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("boom")
        return [{"id": "plugin-1", "title": "红果短剧", "controller": FakeSpiderController("红果短剧"), "search_enabled": True}]

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[],
        plugin_loader_task=plugin_loader_task,
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    window.resize(920, 520)
    window.show()

    qtbot.waitUntil(lambda: window.startup_plugin_retry_button.isVisible())
    window.startup_plugin_retry_button.click()

    qtbot.waitUntil(lambda: "红果短剧" in [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())])
    assert attempts["count"] == 2


def test_main_window_ignores_late_startup_plugin_results_after_close(qtbot) -> None:
    release_load = threading.Event()

    def plugin_loader_task():
        assert release_load.wait(timeout=5), "plugin load was never released"
        return [{"id": "plugin-1", "title": "红果短剧", "controller": FakeSpiderController("红果短剧"), "search_enabled": True}]

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[],
        plugin_loader_task=plugin_loader_task,
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    window.close()
    release_load.set()

    qtbot.wait(100)


def test_main_window_applies_plugin_overflow_after_async_startup_load(qtbot, monkeypatch) -> None:
    def plugin_loader_task():
        return [
            {"id": f"plugin-{index}", "title": f"插件{index}", "controller": FakeSpiderController(f"插件{index}"), "search_enabled": True}
            for index in range(1, 6)
        ]

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[],
        plugin_loader_task=plugin_loader_task,
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_available_plugin_tab_width", lambda: 220)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)

    window.show()

    qtbot.waitUntil(lambda: window.plugin_overflow_button.text() == "更多(3)")
    assert [definition.title for definition in window._hidden_plugin_tab_definitions] == ["插件3", "插件4", "插件5"]


def test_main_window_hides_overflow_plugin_tabs_behind_more_button(qtbot, monkeypatch) -> None:
    controllers = [FakeSpiderController(f"插件{i}") for i in range(1, 6)]
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"id": f"plugin-{index}", "title": f"插件{index}", "controller": controller, "search_enabled": True}
            for index, controller in enumerate(controllers, start=1)
        ],
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_available_plugin_tab_width", lambda: 220)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)

    window.show()
    window._refresh_navigation_tabs()

    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == [
        "豆瓣电影",
        "环球片单",
        "电报影视",
        "网络直播",
        "Emby",
        "Jellyfin",
        "飞牛影视",
        "文件浏览",
        "我的收藏",
        "我的追更",
        "播放记录",
        "插件1",
        "插件2",
    ]
    assert window.plugin_overflow_button.isVisible() is True
    assert window.plugin_overflow_button.text() == "更多(3)"
    assert [definition.title for definition in window._hidden_plugin_tab_definitions] == ["插件3", "插件4", "插件5"]


def test_main_window_keeps_personal_tabs_before_plugins_with_more_button(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        bilibili_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {
                "id": f"plugin-{index}",
                "title": f"插件{index}",
                "controller": FakeSpiderController(f"插件{index}"),
                "search_enabled": True,
            }
            for index in range(1, 4)
        ],
        plugin_manager=WidthAwarePluginManager(),
        show_bilibili_tab=True,
    )

    qtbot.addWidget(window)
    window.resize(1280, 520)
    monkeypatch.setattr(window, "_available_plugin_tab_width", lambda: 0)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)
    window.show()
    window._refresh_navigation_tabs()

    assert window.plugin_overflow_button.isVisible() is True
    tab_titles = [
        window.nav_tabs.tabText(index)
        for index in range(window.nav_tabs.count())
    ]
    personal_indices = [
        tab_titles.index(title)
        for title in ["文件浏览", "我的收藏", "我的追更", "播放记录"]
    ]
    visible_plugin_indices = [
        index
        for index, title in enumerate(tab_titles)
        if title.startswith("插件")
    ]

    assert personal_indices == sorted(personal_indices)
    assert visible_plugin_indices == [] or (
        max(personal_indices) < min(visible_plugin_indices)
    )
    assert all(
        window.nav_tabs.tab_bar.tabRect(index).right()
        <= window.nav_tabs.tab_bar.width()
        for index in personal_indices
    )


def test_main_window_hides_more_button_when_all_plugin_tabs_fit(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"id": "plugin-1", "title": "插件1", "controller": FakeSpiderController("插件1"), "search_enabled": True},
            {"id": "plugin-2", "title": "插件2", "controller": FakeSpiderController("插件2"), "search_enabled": True},
        ],
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_available_plugin_tab_width", lambda: 600)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)

    window.show()
    window._refresh_navigation_tabs()

    assert window.plugin_overflow_button.isVisible() is False
    assert window._hidden_plugin_tab_definitions == []


def test_main_window_available_plugin_width_reserves_more_button_space_when_overflowing(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"id": "plugin-1", "title": "插件1", "controller": FakeSpiderController("插件1"), "search_enabled": True},
            {"id": "plugin-2", "title": "插件2", "controller": FakeSpiderController("插件2"), "search_enabled": True},
            {"id": "plugin-3", "title": "插件3", "controller": FakeSpiderController("插件3"), "search_enabled": True},
        ],
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)
    monkeypatch.setattr(window, "_plugin_overflow_button_width", lambda: 84)
    monkeypatch.setattr(window.nav_tabs.tab_bar, "width", lambda: 804)
    monkeypatch.setattr(window.nav_tabs, "width", lambda: 804)

    assert window._available_plugin_tab_width() == 0


def test_main_window_hides_all_plugin_tabs_when_fixed_tabs_exhaust_width(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"id": "plugin-1", "title": "插件1", "controller": FakeSpiderController("插件1"), "search_enabled": True},
            {"id": "plugin-2", "title": "插件2", "controller": FakeSpiderController("插件2"), "search_enabled": True},
            {"id": "plugin-3", "title": "插件3", "controller": FakeSpiderController("插件3"), "search_enabled": True},
        ],
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_available_plugin_tab_width", lambda: 0)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)

    window.show()
    window._refresh_navigation_tabs()

    assert "插件1" not in [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())]
    assert [definition.title for definition in window._hidden_plugin_tab_definitions] == ["插件1", "插件2", "插件3"]
    assert window.plugin_overflow_button.text() == "更多(3)"


def test_main_window_does_not_lock_width_to_all_visible_plugin_tabs(qtbot) -> None:
    baseline_window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(baseline_window)
    baseline_window.resize(640, 480)
    baseline_window.show()
    baseline_width = baseline_window.width()
    baseline_window.close()

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"id": f"plugin-{index}", "title": f"插件{index}", "controller": FakeSpiderController(f"插件{index}"), "search_enabled": True}
            for index in range(1, 31)
        ],
        plugin_manager=WidthAwarePluginManager(),
    )

    qtbot.addWidget(window)
    window.resize(640, 480)
    window.show()

    assert window.width() == baseline_width
    assert len(window._hidden_plugin_tab_definitions) > 0


def test_main_window_plugin_overflow_drawer_filters_hidden_plugins(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"id": "plugin-1", "title": "短剧一号", "controller": FakeSpiderController("短剧一号"), "search_enabled": True},
            {"id": "plugin-2", "title": "短剧二号", "controller": FakeSpiderController("短剧二号"), "search_enabled": True},
            {"id": "plugin-3", "title": "音乐插件", "controller": FakeSpiderController("音乐插件"), "search_enabled": True},
        ],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_available_plugin_tab_width", lambda: 100)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)

    window.show()
    window._refresh_navigation_tabs()
    window._open_plugin_overflow_drawer()

    assert [item.text() for item in window._plugin_overflow_drawer.visible_items()] == ["短剧二号", "音乐插件"]

    window._plugin_overflow_drawer.search_edit.setText("音乐")

    assert [item.text() for item in window._plugin_overflow_drawer.visible_items()] == ["音乐插件"]


def test_main_window_selecting_hidden_plugin_from_drawer_switches_content_without_rebuilding_pages(
    qtbot, monkeypatch,
) -> None:
    controllers = [CountingSpiderController(f"插件{i}") for i in range(1, 4)]
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"id": f"plugin-{index}", "title": f"插件{index}", "controller": controller, "search_enabled": True}
            for index, controller in enumerate(controllers, start=1)
        ],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_available_plugin_tab_width", lambda: 100)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)

    window.show()
    original_pages = [page for page, _controller, _plugin_id in window._plugin_pages]
    window._refresh_navigation_tabs()
    window._open_plugin_overflow_drawer()

    window._plugin_overflow_drawer.select_plugin_by_title("插件3")

    assert window._active_widget is original_pages[2]
    assert window.nav_tabs.currentWidget() is original_pages[2]
    assert window._plugin_overflow_drawer.isVisible() is True
    assert window.plugin_overflow_button.isChecked() is True
    assert window.nav_tabs.tab_bar.property("hiddenPluginActive") is True
    assert window._plugin_pages[2][0] is original_pages[2]
    assert controllers[2].load_calls <= 1

    window._open_plugin_overflow_drawer()
    window.plugin_overflow_button.click()

    assert window._plugin_overflow_drawer.isVisible() is False
    assert window.plugin_overflow_button.isChecked() is True

    window.nav_tabs.setCurrentWidget(window.douban_page)

    assert window.plugin_overflow_button.isChecked() is False
    assert window.nav_tabs.tab_bar.property("hiddenPluginActive") is False


def test_main_window_resize_keeps_active_hidden_plugin_page_instance(qtbot, monkeypatch) -> None:
    controllers = [CountingSpiderController(f"插件{i}") for i in range(1, 4)]
    available_width = {"value": 100}
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"id": f"plugin-{index}", "title": f"插件{index}", "controller": controller, "search_enabled": True}
            for index, controller in enumerate(controllers, start=1)
        ],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_available_plugin_tab_width", lambda: available_width["value"])
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)

    window.show()
    original_pages = [page for page, _controller, _plugin_id in window._plugin_pages]
    window._refresh_navigation_tabs()
    window._open_plugin_overflow_drawer()
    window._plugin_overflow_drawer.select_plugin_by_title("插件3")

    available_width["value"] = 600
    window._refresh_navigation_tabs()

    assert window.nav_tabs.currentWidget() is original_pages[2]
    assert window._plugin_pages[2][0] is original_pages[2]
    assert controllers[2].load_calls <= 1


def test_main_window_plugin_tab_context_menu_reload_refreshes_changed_plugin(qtbot, monkeypatch) -> None:
    manager = WidthAwarePluginManager()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=manager.load_plugins(["1", "2", "3"]),
        plugin_manager=manager,
    )

    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_available_plugin_tab_width", lambda: 600)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)
    window.show()
    window._refresh_navigation_tabs()

    result = window._run_plugin_context_action("refresh", "1")

    assert result is True
    assert manager.refresh_calls == [1]
    assert manager.load_plugins_calls[-1] == ["1"]
    assert "插件1" in [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())]


def test_main_window_hidden_plugin_context_menu_rename_updates_drawer_items(qtbot, monkeypatch) -> None:
    manager = WidthAwarePluginManager()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=manager.load_plugins(["1", "2", "3"]),
        plugin_manager=manager,
    )

    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_available_plugin_tab_width", lambda: 100)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)

    window.show()
    window._refresh_navigation_tabs()
    window._open_plugin_overflow_drawer()
    monkeypatch.setattr(window._plugin_actions, "prompt_display_name", lambda parent, current: "重命名插件")

    result = window._run_plugin_context_action("rename", "2")

    assert result is True
    assert manager.rename_calls == [(2, "重命名插件")]
    assert [item.text() for item in window._plugin_overflow_drawer.visible_items()] == ["重命名插件", "插件3"]


def test_main_window_hidden_plugin_reload_keeps_active_hidden_plugin(qtbot, monkeypatch) -> None:
    manager = WidthAwarePluginManager()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=manager.load_plugins(["1", "2", "3"]),
        plugin_manager=manager,
    )

    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_available_plugin_tab_width", lambda: 100)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)

    window.show()
    window._refresh_navigation_tabs()
    window._open_plugin_overflow_drawer()
    window._plugin_overflow_drawer.select_plugin_by_title("插件3")
    active_before_reload = window.nav_tabs.currentWidget()

    result = window._run_plugin_context_action("refresh", "3")

    assert result is True
    assert manager.refresh_calls == [3]
    assert manager.load_plugins_calls[-1] == ["3"]
    assert window.nav_tabs.currentWidget() is not active_before_reload
    assert window.nav_tabs.currentWidget() is window._plugin_pages[2][0]
    assert window.plugin_overflow_button.isChecked() is True
    assert window.nav_tabs.tab_bar.property("hiddenPluginActive") is True


def test_main_window_manage_categories_context_action_reloads_only_target_plugin(qtbot, monkeypatch) -> None:
    manager = WidthAwarePluginManager()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=manager.load_plugins(["1", "2", "3"]),
        plugin_manager=manager,
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_available_plugin_tab_width", lambda: 600)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)
    opened: list[int] = []
    monkeypatch.setattr(window, "_open_plugin_category_manager", lambda plugin_id: opened.append(plugin_id) or True)

    window.show()
    window._refresh_navigation_tabs()
    result = window._run_plugin_context_action("manage_categories", "2")

    assert result is True
    assert opened == [2]
    assert manager.load_plugins_calls[-1] == ["2"]


def test_main_window_plugin_context_menu_includes_category_management(qtbot, monkeypatch) -> None:
    manager = WidthAwarePluginManager()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=manager.load_plugins(["1"]),
        plugin_manager=manager,
    )
    qtbot.addWidget(window)
    captured_actions: list[str] = []

    class FakeAction:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeMenu:
        def __init__(self, parent=None) -> None:
            del parent
            self.actions: list[FakeAction] = []

        def addAction(self, text: str):
            action = FakeAction(text)
            self.actions.append(action)
            captured_actions.append(text)
            return action

        def exec(self, global_pos):
            del global_pos
            return None

    monkeypatch.setattr(main_window_module, "QMenu", FakeMenu)

    window._open_plugin_context_menu("1", window.mapToGlobal(window.rect().center()))

    assert captured_actions == ["重新加载", "编辑名称", "编辑配置", "分类管理", "禁用"]


def _capture_context_menu_actions(monkeypatch) -> list[str]:
    captured_actions: list[str] = []

    class FakeMenu:
        def __init__(self, parent=None) -> None:
            del parent
            self.actions: list[object] = []

        def addAction(self, text: str):
            captured_actions.append(text)
            action = object()
            self.actions.append(action)
            return action

        def exec(self, global_pos):
            del global_pos
            return None

    monkeypatch.setattr(main_window_module, "QMenu", FakeMenu)
    return captured_actions


def test_main_window_video_context_menu_shows_all_actions_for_normal_source(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )
    qtbot.addWidget(window)
    captured_actions = _capture_context_menu_actions(monkeypatch)

    window._open_video_item_context_menu(
        window.telegram_page,
        VodItem(vod_id="vod-1", vod_name="测试影片"),
        window.mapToGlobal(window.rect().center()),
    )

    assert captured_actions == ["打开播放", "全局搜索", "加入收藏"]


def test_main_window_video_context_menu_shows_only_global_search_for_douban(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )
    qtbot.addWidget(window)
    captured_actions = _capture_context_menu_actions(monkeypatch)

    window._open_video_item_context_menu(
        window.douban_page,
        VodItem(vod_id="db-1", vod_name="豆瓣条目"),
        window.mapToGlobal(window.rect().center()),
    )

    assert captured_actions == ["全局搜索"]


def test_main_window_video_context_menu_hides_global_search_for_live(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )
    qtbot.addWidget(window)
    captured_actions = _capture_context_menu_actions(monkeypatch)

    window._open_video_item_context_menu(
        window.live_page,
        VodItem(vod_id="live-1", vod_name="直播间"),
        window.mapToGlobal(window.rect().center()),
    )

    assert captured_actions == ["打开播放", "加入收藏"]


def test_main_window_video_context_menu_hides_global_search_in_external_results_mode(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )
    qtbot.addWidget(window)
    captured_actions = _capture_context_menu_actions(monkeypatch)
    result_item = VodItem(vod_id="vod-1", vod_name="搜索结果")
    window.telegram_page.show_external_results([result_item], total=1, page=1)

    window._open_video_item_context_menu(
        window.telegram_page,
        result_item,
        window.mapToGlobal(window.rect().center()),
    )

    assert captured_actions == ["打开播放", "加入收藏"]


def test_main_window_video_context_menu_open_uses_page_click_handler(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )
    qtbot.addWidget(window)
    clicked_items: list[str] = []
    monkeypatch.setattr(
        window.telegram_page,
        "_handle_card_clicked",
        lambda item: clicked_items.append(item.vod_id),
    )

    window._handle_video_item_context_open(
        window.telegram_page,
        VodItem(vod_id="vod-1", vod_name="测试影片"),
    )

    assert clicked_items == ["vod-1"]


def test_main_window_video_context_menu_global_search_uses_item_title(qtbot, monkeypatch) -> None:
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    started_keywords: list[str] = []
    monkeypatch.setattr(window, "_start_global_search", lambda: started_keywords.append(window.global_search_edit.text()))

    window._handle_video_item_context_global_search(VodItem(vod_id="vod-1", vod_name="成何体统"))

    assert window.global_search_edit.text() == "成何体统"
    assert started_keywords == ["成何体统"]


def test_main_window_player_detail_name_global_search_signal_starts_global_search(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def __init__(self) -> None:
            self.callbacks = []

        def connect(self, callback) -> None:
            self.callbacks.append(callback)

        def emit(self, *args) -> None:
            for callback in list(self.callbacks):
                callback(*args)

    class RecordingPlayerWindow:
        last_instance = None

        def __init__(self, controller, config, save_config, **kwargs) -> None:
            self.opened: list[tuple[object, bool]] = []
            self.closed_to_main = FakeSignal()
            self.global_search_requested = FakeSignal()
            RecordingPlayerWindow.last_instance = self

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    started_keywords: list[str] = []
    monkeypatch.setattr(window, "_start_global_search", lambda: started_keywords.append(window.global_search_edit.text()))
    request = OpenPlayerRequest(
        vod=VodItem(vod_id="movie-1", vod_name="播放详情"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
        clicked_index=0,
        source_kind="browse",
        source_vod_id="movie-1",
    )

    window.open_player(request)
    qtbot.waitUntil(lambda: RecordingPlayerWindow.last_instance is not None)
    RecordingPlayerWindow.last_instance.global_search_requested.emit("刮削后的标题")

    assert window.global_search_edit.text() == "刮削后的标题"
    assert started_keywords == ["刮削后的标题"]


def test_main_window_following_detail_related_global_search_signal_starts_global_search(qtbot, monkeypatch) -> None:
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    started_keywords: list[str] = []
    monkeypatch.setattr(window, "_start_global_search", lambda: started_keywords.append(window.global_search_edit.text()))

    window.following_detail_page.related_global_search_requested.emit("Gen V")

    assert window.global_search_edit.text() == "Gen V"
    assert started_keywords == ["Gen V"]


def test_main_window_following_detail_related_media_signal_opens_universal_detail(qtbot, monkeypatch) -> None:
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    opened = []
    window.following_detail_page.related_media_detail_requested.disconnect()
    window.following_detail_page.related_media_detail_requested.connect(opened.append)

    identity = MediaDetailIdentity(media_type="tv", tmdb_id="100", title="Gen V")
    window.following_detail_page.related_media_detail_requested.emit(identity)

    assert opened == [identity]


def test_main_window_following_detail_related_lookup_opens_universal_detail(qtbot) -> None:
    detail_controller = FakeMediaDetailController()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        media_detail_controller=detail_controller,
    )
    qtbot.addWidget(window)

    lookup = MediaDetailLookup(
        title="Gen V",
        year="2023",
        provider="official_douban",
        provider_id="36454318",
        poster_url="https://img.example/poster.jpg",
    )
    window.following_detail_page.related_media_detail_requested.emit(lookup)

    assert window.nav_tabs.currentWidget() is window.media_detail_page
    assert window.media_detail_page.title_label.text() == "Gen V"
    assert window.media_detail_page.status_label.text() == "正在加载元数据..."
    qtbot.waitUntil(lambda: detail_controller.lookup_calls == [lookup], timeout=1000)


def test_main_window_media_detail_page_uses_following_episode_grid_config(qtbot) -> None:
    config = AppConfig(following_episode_grid_columns=3)
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
    )
    qtbot.addWidget(window)

    assert window.media_detail_page.episode_browser.grid_columns() == 3


def test_main_window_video_context_menu_favorite_adds_item_to_favorites(qtbot) -> None:
    favorites = FakeFavoritesController()
    window = MainWindow(
        telegram_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        favorites_controller=favorites,
    )
    qtbot.addWidget(window)

    window._handle_video_item_context_favorite(
        window.telegram_page,
        VodItem(vod_id="vod-1", vod_name="测试影片", vod_pic="poster.jpg", vod_remarks="更新至第1集"),
    )

    assert len(favorites.add_calls) == 1
    payload = favorites.add_calls[0]
    assert {
        key: payload[key]
        for key in (
            "source_kind",
            "source_key",
            "source_name",
            "vod_id",
            "vod_name_snapshot",
            "latest_vod_name",
            "vod_pic",
            "vod_remarks",
            "title_changed",
        )
    } == {
        "source_kind": "telegram",
        "source_key": "",
        "source_name": "电报影视",
        "vod_id": "vod-1",
        "vod_name_snapshot": "测试影片",
        "latest_vod_name": "测试影片",
        "vod_pic": "poster.jpg",
        "vod_remarks": "更新至第1集",
        "title_changed": False,
    }
    assert isinstance(payload["created_at"], int)
    assert isinstance(payload["updated_at"], int)


def test_main_window_reports_heat_favorite_add_from_video_context(qtbot) -> None:
    favorites = FakeFavoritesController()
    heat = FakeHeatController()
    window = MainWindow(
        telegram_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        favorites_controller=favorites,
        heat_controller=heat,
    )
    qtbot.addWidget(window)

    window._handle_video_item_context_favorite(
        window.telegram_page,
        VodItem(vod_id="vod-1", vod_name="测试影片", vod_pic="poster.jpg"),
    )

    assert len(favorites.add_calls) == 1
    assert len(heat.media_events) == 1
    event_type, media, context = heat.media_events[0]
    assert event_type == "favorite_add"
    assert media.title == "测试影片"
    assert context == {"source_kind": "telegram", "source_key": ""}


def test_main_window_player_favorite_uses_plugin_display_name(qtbot) -> None:
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {
                "id": "1",
                "title": "插件1",
                "controller": FakeSpiderController("插件1"),
                "search_enabled": True,
            }
        ],
        favorites_controller=FakeFavoritesController(),
    )
    qtbot.addWidget(window)
    window.player_window = SimpleNamespace(
        session=SimpleNamespace(
            vod=VodItem(vod_id="vod-1", vod_name="测试影片", vod_pic="poster.jpg", vod_remarks="完结"),
            source_kind="plugin",
            source_key="1",
        )
    )

    payload = window._current_player_favorite_payload(PlayItem(title="第1集", url="https://media.example/1.m3u8"))

    assert payload is not None
    assert payload["source_kind"] == "plugin"
    assert payload["source_key"] == "1"
    assert payload["source_name"] == "插件1"


def test_main_window_player_favorite_payload_includes_tmdb_identity_from_detail_fields(qtbot) -> None:
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        favorites_controller=FakeFavoritesController(),
    )
    qtbot.addWidget(window)
    window.player_window = SimpleNamespace(
        session=SimpleNamespace(
            vod=VodItem(
                vod_id="vod-1",
                vod_name="黑袍纠察队",
                vod_pic="poster.jpg",
                vod_remarks="完结",
                type_name="电视剧",
                detail_fields=[PlaybackDetailField(label="TMDB ID", value="76479")],
            ),
            source_kind="browse",
            source_key="",
        )
    )

    payload = window._current_player_favorite_payload(PlayItem(title="第1集", url="https://media.example/1.m3u8"))

    assert payload is not None
    assert payload["tmdb_provider_id"] == "tv:76479"
    assert payload["tmdb_id"] == "76479"
    assert payload["tmdb_media_type"] == "tv"


def test_main_window_reports_heat_favorite_add_from_player(qtbot) -> None:
    favorites = FakeFavoritesController()
    heat = FakeHeatController()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        favorites_controller=favorites,
        heat_controller=heat,
    )
    qtbot.addWidget(window)
    item = PlayItem(title="第1集", url="https://media.example/1.m3u8", vod_id="vod-1")
    window.player_window = SimpleNamespace(
        session=SimpleNamespace(
            vod=VodItem(vod_id="vod-1", vod_name="黑袍纠察队", vod_pic="poster.jpg"),
            source_kind="browse",
            source_key="",
        )
    )

    window._toggle_player_item_favorite(item)

    assert len(favorites.add_calls) == 1
    assert len(heat.media_events) == 1
    event_type, media, context = heat.media_events[0]
    assert event_type == "favorite_add"
    assert media.title == "黑袍纠察队"
    assert context == {"source_kind": "browse", "source_key": ""}


def test_main_window_favorites_page_resolves_saved_plugin_source_name(qtbot) -> None:
    class Controller:
        def load_page(self, *, page: int, size: int, keyword: str):
            record = FavoriteRecord(
                source_kind="plugin",
                source_key="1",
                source_name="插件",
                vod_id="vod-1",
                vod_name_snapshot="测试影片",
                latest_vod_name="测试影片",
                vod_pic="",
                vod_remarks="",
                title_changed=False,
                created_at=10,
                updated_at=10,
            )
            return [
                FavoriteCardItem(
                    record=record,
                    display_title="测试影片",
                    source_label=record.source_name,
                    updated_hint=False,
                    secondary_text="",
                )
            ], 1

    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {
                "id": "1",
                "title": "插件1",
                "controller": FakeSpiderController("插件1"),
                "search_enabled": True,
            }
        ],
        favorites_controller=Controller(),
    )
    qtbot.addWidget(window)

    window.favorites_page.ensure_loaded()
    qtbot.waitUntil(lambda: len(window.favorites_page.card_widgets) == 1)

    assert window.favorites_page.card_widgets[0].source_label.text() == "插件1"


def test_main_window_history_context_favorite_adds_item_to_favorites(qtbot) -> None:
    favorites = FakeFavoritesController()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=DummyHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {
                "id": "1",
                "title": "插件1",
                "controller": FakeSpiderController("插件1"),
                "search_enabled": True,
            }
        ],
        favorites_controller=favorites,
    )
    qtbot.addWidget(window)

    window._handle_history_context_favorite(
        HistoryRecord(
            id=1,
            key="vod-1",
            vod_name="测试影片",
            vod_pic="poster.jpg",
            vod_remarks="完结",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=10,
            source_kind="spider_plugin",
            source_plugin_id=1,
            source_plugin_name="插件1",
            source_key="1",
        )
    )

    assert len(favorites.add_calls) == 1
    payload = favorites.add_calls[0]
    assert {
        "source_kind": payload["source_kind"],
        "source_key": payload["source_key"],
        "source_name": payload["source_name"],
        "vod_id": payload["vod_id"],
        "vod_name_snapshot": payload["vod_name_snapshot"],
        "latest_vod_name": payload["latest_vod_name"],
        "vod_pic": payload["vod_pic"],
        "vod_remarks": payload["vod_remarks"],
        "title_changed": payload["title_changed"],
    } == {
        "source_kind": "plugin",
        "source_key": "1",
        "source_name": "插件1",
        "vod_id": "vod-1",
        "vod_name_snapshot": "测试影片",
        "latest_vod_name": "测试影片",
        "vod_pic": "poster.jpg",
        "vod_remarks": "完结",
        "title_changed": False,
    }


def test_main_window_disabling_active_plugin_falls_back_to_first_visible_tab(qtbot, monkeypatch) -> None:
    manager = WidthAwarePluginManager()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=manager.load_plugins(["1", "2", "3"]),
        plugin_manager=manager,
    )

    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_available_plugin_tab_width", lambda: 600)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)
    window.show()
    window._refresh_navigation_tabs()
    window.nav_tabs.setCurrentWidget(window._plugin_pages[1][0])

    result = window._run_plugin_context_action("toggle_enabled", "2")

    assert result is True
    assert manager.toggle_calls == [(2, False)]
    assert window.nav_tabs.currentWidget() is window.douban_page


def test_main_window_uses_centered_rounded_search_box_with_icon_controls(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.global_search_edit.parentWidget() is not None
    assert window.global_search_container.width() == 400
    assert window.global_search_container.minimumWidth() == 400
    assert window.global_search_container.maximumWidth() == 400
    assert window.global_search_edit.placeholderText() == "搜索"
    assert window.global_search_edit.isClearButtonEnabled() is True
    assert window.global_search_edit.styleSheet()
    assert "border-radius: 18px;" in window.global_search_edit.styleSheet()
    assert window.global_search_button.text() == ""
    assert window.global_search_button.icon().isNull() is False
    assert window.global_search_clear_button.isHidden() is True
    assert window.header_layout.indexOf(window.global_search_container) < window.header_layout.indexOf(window.plugin_manager_button)


def test_main_window_global_search_shows_only_tabs_with_results_and_count_titles(qtbot) -> None:
    telegram = SearchableController([_vod("Telegram One")], total=12)
    emby = SearchableController([])
    jellyfin = SearchableController([_vod("Jellyfin One")], total=3)
    feiniu = SearchableController([])
    plugin_controller = SearchableController([_vod("Plugin One"), _vod("Plugin Two")], total=2)

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=emby,
        jellyfin_controller=jellyfin,
        feiniu_controller=feiniu,
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"id": "plugin-a", "title": "红果短剧", "controller": plugin_controller, "search_enabled": False},
        ],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()

    qtbot.waitUntil(
        lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == [
            "电报影视(12)",
            "Jellyfin(3)",
            "红果短剧(2)",
        ]
    )
    assert telegram.search_calls == [("庆余年", 1)]
    assert emby.search_calls == [("庆余年", 1)]
    assert jellyfin.search_calls == [("庆余年", 1)]
    assert feiniu.search_calls == [("庆余年", 1)]
    assert plugin_controller.search_calls == [("庆余年", 1)]


def test_main_window_global_search_overflow_results_use_more_button(qtbot, monkeypatch) -> None:
    controllers = [SearchableController([_vod(f"结果{i}")], total=i) for i in range(1, 8)]
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=controllers[0],
        live_controller=FakeStaticController(),
        emby_controller=controllers[1],
        jellyfin_controller=controllers[2],
        feiniu_controller=controllers[3],
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"id": f"plugin-{index}", "title": f"插件{index}", "controller": controller, "search_enabled": True}
            for index, controller in enumerate(controllers[4:], start=1)
        ],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_plugin_tab_title_width", lambda title: 88)
    monkeypatch.setattr(window.nav_tabs.tab_bar, "width", lambda: 240)
    monkeypatch.setattr(window.nav_tabs, "width", lambda: 240)
    window.show()

    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: window.plugin_overflow_button.isVisible() is True)
    assert window.plugin_overflow_button.text() == "更多(6)"
    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == [
        "电报影视(1)",
    ]
    assert [definition.title for definition in window._hidden_plugin_tab_definitions] == [
        "Emby",
        "Jellyfin",
        "飞牛影视",
        "插件1",
        "插件2",
        "插件3",
    ]
    window._open_plugin_overflow_drawer()
    assert [item.text() for item in window._plugin_overflow_drawer.visible_items()] == [
        "Emby(2)",
        "Jellyfin(3)",
        "飞牛影视(4)",
        "插件1(5)",
        "插件2(6)",
        "插件3(7)",
    ]
    window._plugin_overflow_drawer.select_plugin_by_title("Emby(2)")

    assert window.nav_tabs.currentWidget() is window.emby_page


def test_main_window_global_search_hides_all_tabs_then_shows_results_incrementally(qtbot) -> None:
    telegram = AsyncKeywordSearchController({"庆余年": ([_vod("Telegram One")], 12)})
    plugin_controller = AsyncKeywordSearchController({"庆余年": ([_vod("Plugin One")], 1)})

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"id": "plugin-a", "title": "红果短剧", "controller": plugin_controller, "search_enabled": False},
        ],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: telegram.search_calls == [("庆余年", 1)])
    assert window.nav_tabs.count() == 0

    telegram.release("庆余年")
    qtbot.waitUntil(lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == ["电报影视(12)"])

    plugin_controller.release("庆余年")
    qtbot.waitUntil(lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == ["电报影视(12)", "红果短剧(1)"])


def test_main_window_switching_result_tabs_keeps_global_search_results(qtbot) -> None:
    telegram = SearchableCategoryController(
        category_item=_vod("分类视频"),
        search_items=[_vod("Telegram Result", remarks="搜索结果")],
        total=1,
    )
    emby = SearchableCategoryController(
        category_item=_vod("Emby 分类视频"),
        search_items=[_vod("Emby Result", remarks="搜索结果")],
        total=1,
    )

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=emby,
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == ["电报影视(1)", "Emby(1)"])
    assert [button.text() for button in window.telegram_page.card_buttons] == ["Telegram Result\n搜索结果"]
    assert [button.text() for button in window.emby_page.card_buttons] == ["Emby Result\n搜索结果"]

    window.nav_tabs.setCurrentIndex(1)
    qtbot.wait(100)

    assert [button.text() for button in window.emby_page.card_buttons] == ["Emby Result\n搜索结果"]
    assert window.emby_page.category_list.isHidden() is True
    assert emby.load_categories_calls == 0
    assert emby.load_items_calls == []


def test_main_window_global_search_results_can_paginate_current_source_only(qtbot) -> None:
    telegram = PagedSearchableController(
        {
            1: ([_vod(f"Telegram Page 1-{index}") for index in range(30)], 61),
            2: ([_vod(f"Telegram Page 2-{index}") for index in range(30)], 61),
        }
    )
    emby = SearchableController([_vod("Emby Result")], total=1)

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=emby,
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == ["电报影视(61)", "Emby(1)"])
    assert [button.text() for button in window.telegram_page.card_buttons] == [f"Telegram Page 1-{index}" for index in range(30)]
    assert telegram.search_calls == [("庆余年", 1)]
    assert emby.search_calls == [("庆余年", 1)]

    window.telegram_page.next_page()

    qtbot.waitUntil(lambda: [button.text() for button in window.telegram_page.card_buttons] == [f"Telegram Page 2-{index}" for index in range(30)])
    assert telegram.search_calls == [("庆余年", 1), ("庆余年", 2)]
    assert emby.search_calls == [("庆余年", 1)]
    assert window.telegram_page.page_label.text() == "第 2 / 3 页"


def test_main_window_global_search_uses_total_count_for_tab_and_pagecount_for_pagination(qtbot) -> None:
    telegram = PageInfoSearchableController()

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == ["电报影视(61)"])
    assert window.telegram_page.page_label.text() == "第 1 / 2 页"

    window.telegram_page.next_page()

    qtbot.waitUntil(lambda: [button.text() for button in window.telegram_page.card_buttons] == ["Telegram Page 2"])
    assert telegram.search_calls == [("庆余年", 1), ("庆余年", 2)]
    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == ["电报影视(61)"]
    assert window.telegram_page.page_label.text() == "第 2 / 2 页"


def test_main_window_global_search_prefers_inferred_page_size(qtbot) -> None:
    telegram = VariablePageSizeSearchableController(
        {
            1: ([ _vod(f"Telegram Page 1-{index}") for index in range(20) ], 41),
            2: ([ _vod("Telegram Page 2-last") ], 41),
        }
    )

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == ["电报影视(41)"])
    assert window.telegram_page.page_label.text() == "第 1 / 3 页"
    assert window.telegram_page.next_page_button.isEnabled() is True

    window.telegram_page.next_page()

    qtbot.waitUntil(lambda: [button.text() for button in window.telegram_page.card_buttons] == ["Telegram Page 2-last"])
    assert window.telegram_page.page_label.text() == "第 2 / 3 页"


def test_main_window_global_search_includes_playback_history_results(qtbot) -> None:
    history = GlobalSearchHistoryController([_history_record("庆余年")])

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=history,
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == ["播放记录(1)"])
    assert history.load_calls == [(1, 100, "庆余年")]
    assert window.global_history_page.table.item(0, 0).text() == "庆余年"
    assert window.global_history_page.table.item(0, 5).text() == "电报影视"


def test_global_search_includes_smart_match_tab_when_controller_present(qtbot) -> None:
    smart_controller = SmartSearchGlobalController()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=DummyHistoryController(),
        player_controller=FakePlayerController(),
        telegram_controller=SearchableController([]),
        smart_search_controller=smart_controller,
        config=AppConfig(),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("类似黑镜的高分科幻")
    window._start_global_search()

    qtbot.waitUntil(lambda: smart_controller.calls == [("类似黑镜的高分科幻", 1)], timeout=1000)
    qtbot.waitUntil(
        lambda: any(
            window.nav_tabs.tabText(i) == "智能匹配(1)"
            for i in range(window.nav_tabs.count())
        ),
        timeout=1000,
    )


def test_following_search_play_skips_smart_match_controller(qtbot) -> None:
    telegram = SearchableController([_vod("Telegram Result")], total=1)
    smart_controller = SmartSearchGlobalController()
    following = FakeFollowingController()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=DummyHistoryController(),
        player_controller=FakePlayerController(),
        telegram_controller=telegram,
        following_controller=following,
        smart_search_controller=smart_controller,
        config=AppConfig(),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window.show()

    window.search_play_for_following(1)

    qtbot.waitUntil(lambda: telegram.search_calls == [("凡人修仙传", 1)], timeout=1000)
    qtbot.wait(50)
    assert smart_controller.calls == []
    assert all(
        window.nav_tabs.tabText(index) != "智能匹配(1)"
        for index in range(window.nav_tabs.count())
    )


def test_main_window_global_search_history_result_opens_existing_history_route(qtbot, monkeypatch) -> None:
    history = GlobalSearchHistoryController([_history_record("庆余年", key="telegram-detail-1")])
    telegram = FakeSpiderController("电报影视")

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=history,
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )
    opened: list[OpenPlayerRequest] = []
    monkeypatch.setattr(window, "_start_open_request", lambda builder: opened.append(builder()) or len(opened))

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: window.global_history_page.table.rowCount() == 1)
    window.global_history_page._open_selected(0, 0)

    assert telegram.open_calls == ["telegram-detail-1"]
    assert opened[0].source_vod_id == "telegram-detail-1"


def test_main_window_global_search_popup_does_not_open_on_focus(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=["庆余年", "琅琊榜"]),
        global_search_hotkey_loader=lambda hot_type: [{"title": "热搜一", "query": "热搜一"}],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    window.global_search_edit.setFocus()
    qtbot.wait(50)

    assert window._global_search_popup is None or window._global_search_popup.isVisible() is False


def test_main_window_global_search_popup_button_opens_history_and_default_hot_tab(qtbot) -> None:
    hotkey_calls: list[str] = []
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=["庆余年", "琅琊榜"]),
        global_search_hotkey_loader=lambda hot_type: hotkey_calls.append(hot_type) or [
            {"title": "热搜一", "query": "热搜一"},
            {"title": f"综合-{hot_type}", "query": "综合视频"},
        ],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: window._global_search_popup.isVisible() is True)
    qtbot.waitUntil(lambda: hotkey_calls == ["dsp"])
    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["热搜一", "综合-dsp"])
    assert _popup_history_texts(window) == ["庆余年", "琅琊榜"]
    assert _popup_hot_tab_titles(window) == ["综合", "电视剧", "电影", "综艺", "动漫"]
    assert window._global_search_popup.windowTitle() == "全局搜索"
    assert window._global_search_popup.current_hot_tab_type() == "dsp"
    assert window._global_search_popup.hot_tab_bar.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert window._global_search_popup.width() >= 720


def test_global_search_popup_renders_heat_recommendations(qtbot) -> None:
    popup = main_window_module.GlobalSearchPopup()
    qtbot.addWidget(popup)

    popup.set_heat_items(
        [
            SimpleNamespace(
                media_key="tmdb:tv:1399",
                title="权力的游戏",
                poster="",
                reason="23 人正在播放",
            )
        ]
    )

    assert popup.heat_item_texts() == ["权力的游戏"]
    assert popup.heat_item_button("权力的游戏").toolTip() == "23 人正在播放"


def test_heat_recommendations_dialog_renders_poster_cards(qtbot) -> None:
    dialog = main_window_module.HeatRecommendationsDialog()
    qtbot.addWidget(dialog)

    dialog.set_items(
        [
            SimpleNamespace(
                media_key="tmdb:tv:1399",
                title="权力的游戏",
                poster="",
                heat_score=98,
                reason="23 人正在播放",
            )
        ]
    )

    assert dialog.item_titles() == ["权力的游戏"]
    assert dialog.item_button("权力的游戏").text() == "权力的游戏\n热度 98"
    assert dialog.item_button("权力的游戏").toolTip() == "权力的游戏\n热度 98\n23 人正在播放"
    assert dialog.width() >= 920
    assert dialog.height() >= 720


def test_heat_recommendations_dialog_shows_at_most_thirty_cards(qtbot) -> None:
    dialog = main_window_module.HeatRecommendationsDialog()
    qtbot.addWidget(dialog)

    dialog.set_items(
        [
            SimpleNamespace(
                media_key=f"tmdb:movie:{index}",
                title=f"电影 {index:02d}",
                poster="",
                heat_score=100 - index,
            )
            for index in range(35)
        ]
    )

    assert len(dialog.item_titles()) == 30
    assert dialog.item_titles()[0] == "电影 00"
    assert dialog.item_titles()[-1] == "电影 29"


def test_main_window_reports_global_search_event(qtbot) -> None:
    class FakeHeatController:
        def __init__(self) -> None:
            self.searches = []

        def record_search(self, query, *, source_kind="global_search", media=None) -> None:
            self.searches.append((query, source_kind, media))

        def load_recommendations(self, *, limit=24):
            return []

    heat = FakeHeatController()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        heat_controller=heat,
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.global_search_edit.setText("权力的游戏")
    window._start_global_search()

    assert heat.searches == [("权力的游戏", "global_search", None)]


def test_main_window_global_search_popup_loads_heat_recommendations(qtbot) -> None:
    class FakeHeatController:
        def record_search(self, query, *, source_kind="global_search", media=None) -> None:
            return None

        def load_recommendations(self, *, limit=24):
            return [
                SimpleNamespace(
                    media_key="tmdb:tv:1399",
                    title="权力的游戏",
                    poster="",
                    reason="23 人正在播放",
                )
            ]

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        heat_controller=FakeHeatController(),
        global_search_hotkey_loader=lambda hot_type: [],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: window._global_search_popup.heat_item_texts() == ["权力的游戏"])


def test_main_window_clicking_heat_recommendation_reports_recommendation_search_once(qtbot, monkeypatch) -> None:
    class FakeHeatController:
        def __init__(self) -> None:
            self.searches = []

        def record_search(self, query, *, source_kind="global_search", media=None) -> None:
            self.searches.append((query, source_kind, media))

        def load_recommendations(self, *, limit=24):
            return [
                SimpleNamespace(
                    media_key="tmdb:tv:1399",
                    title="权力的游戏",
                    poster="",
                    reason="23 人正在播放",
                )
            ]

    heat = FakeHeatController()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        heat_controller=heat,
        global_search_hotkey_loader=lambda hot_type: [],
        plugin_manager=FakePluginManager(),
    )
    monkeypatch.setattr(window, "_start_global_search", lambda **kwargs: MainWindow._start_global_search(window, **kwargs))

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window._global_search_popup.heat_item_texts() == ["权力的游戏"])
    qtbot.mouseClick(window._global_search_popup.heat_item_button("权力的游戏"), Qt.MouseButton.LeftButton)

    assert heat.searches == [("权力的游戏", "heat_recommendation", None)]


def test_main_window_heat_recommendations_button_opens_poster_dialog(qtbot, monkeypatch) -> None:
    class FakeHeatController:
        def __init__(self) -> None:
            self.searches = []
            self.recommendation_limits = []

        def record_search(self, query, *, source_kind="global_search", media=None) -> None:
            self.searches.append((query, source_kind, media))

        def load_recommendations(self, *, limit=24):
            self.recommendation_limits.append(limit)
            return [
                SimpleNamespace(
                    media_key="tmdb:tv:1399",
                    title="权力的游戏",
                    poster="",
                    watching_now=23,
                    heat_score=98,
                )
            ]

    heat = FakeHeatController()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        heat_controller=heat,
        global_search_hotkey_loader=lambda hot_type: [],
        plugin_manager=FakePluginManager(),
    )
    monkeypatch.setattr(window, "_start_global_search", lambda **kwargs: MainWindow._start_global_search(window, **kwargs))

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.heat_recommendations_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: window._heat_recommendations_dialog is not None)
    dialog = window._heat_recommendations_dialog
    qtbot.waitUntil(lambda: dialog.item_titles() == ["权力的游戏"])
    assert dialog.windowTitle() == "大家在看"
    assert dialog.item_button("权力的游戏").text() == "权力的游戏\n热度 98"

    qtbot.mouseClick(dialog.item_button("权力的游戏"), Qt.MouseButton.LeftButton)

    assert heat.recommendation_limits == [30]
    assert heat.searches == [("权力的游戏", "heat_recommendation", None)]


def test_main_window_heat_recommendations_dialog_fills_short_list_from_douban_tv(qtbot) -> None:
    class FakeHeatController:
        def __init__(self) -> None:
            self.recommendation_limits = []

        def load_recommendations(self, *, limit=24):
            self.recommendation_limits.append(limit)
            return [
                SimpleNamespace(
                    media_key="tmdb:tv:1",
                    title="热剧 1",
                    poster="",
                    heat_score=99,
                ),
                SimpleNamespace(
                    media_key="tmdb:tv:2",
                    title="热剧 2",
                    poster="",
                    heat_score=98,
                ),
            ]

    class FakeDoubanController:
        def __init__(self) -> None:
            self.item_calls = []

        def load_categories(self):
            return [
                SimpleNamespace(type_id="movie", type_name="电影"),
                SimpleNamespace(type_id="tv", type_name="热门电视剧"),
            ]

        def load_items(self, category_id: str, page: int, filters=None):
            self.item_calls.append((category_id, page, filters))
            items = [
                VodItem(
                    vod_id="duplicate",
                    vod_name="热剧 2",
                    vod_pic="duplicate.jpg",
                )
            ]
            items.extend(
                VodItem(
                    vod_id=f"douban-tv-{index}",
                    vod_name=f"豆瓣剧 {index:02d}",
                    vod_pic=f"https://img.example/{index}.jpg",
                    vod_year="2026",
                    dbid=1000 + index,
                )
                for index in range(35)
            )
            return items, 2

    heat = FakeHeatController()
    douban = FakeDoubanController()
    window = MainWindow(
        douban_controller=douban,
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        heat_controller=heat,
        global_search_hotkey_loader=lambda hot_type: [],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.heat_recommendations_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: window._heat_recommendations_dialog is not None)
    dialog = window._heat_recommendations_dialog
    qtbot.waitUntil(lambda: len(dialog.item_titles()) == 30)
    assert heat.recommendation_limits == [30]
    assert ("tv", 1, None) in douban.item_calls
    assert dialog.item_titles()[:3] == ["热剧 1", "热剧 2", "豆瓣剧 00"]
    assert dialog.item_titles()[-1] == "豆瓣剧 27"


def test_load_tencent_hot_searches_reads_rank_item_list(monkeypatch) -> None:
    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "data": {
                    "navItemList": [
                        {"title": "电影", "hotRankResult": None},
                        {
                            "title": "热搜",
                            "hotRankResult": {
                                "rankItemList": [
                                    {"title": "主角"},
                                    {"title": "奔跑吧 第10季"},
                                ]
                            },
                        },
                    ]
                }
            }

    monkeypatch.setattr(main_window_module.httpx, "post", lambda *args, **kwargs: _FakeResponse())

    assert load_tencent_hot_searches("hot") == [
        {"title": "主角", "query": "主角"},
        {"title": "奔跑吧 第10季", "query": "奔跑吧 第10季"},
    ]


def test_load_tencent_hot_search_sections_filters_to_tabs_with_results(monkeypatch) -> None:
    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "data": {
                    "navItemList": [
                        {
                            "tabName": "热搜",
                            "hotRankResult": {
                                "rankItemList": [{"title": "主角"}],
                            },
                        },
                        {
                            "tabName": "电视剧",
                            "hotRankResult": {
                                "rankItemList": [{"title": "爱情没有神话"}],
                            },
                        },
                        {
                            "tabName": "电影",
                            "hotRankResult": None,
                        },
                    ]
                }
            }

    monkeypatch.setattr(main_window_module.httpx, "post", lambda *args, **kwargs: _FakeResponse())

    categories, items_by_category = load_tencent_hot_search_sections()

    assert categories == [("hot", "热搜"), ("movie", "电视剧")]
    assert items_by_category == {
        "hot": [{"title": "主角", "query": "主角"}],
        "movie": [{"title": "爱情没有神话", "query": "爱情没有神话"}],
    }


def test_main_window_global_search_popup_switches_hot_tabs_and_caches_results(qtbot) -> None:
    hotkey_calls: list[str] = []

    def hotkey_loader(hot_type: str) -> list[dict[str, str]]:
        hotkey_calls.append(hot_type)
        return [{"title": f"{hot_type}-热搜", "query": f"{hot_type}-查询"}]

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=["庆余年"]),
        global_search_hotkey_loader=hotkey_loader,
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: hotkey_calls == ["dsp"])
    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["dsp-热搜"])

    window._global_search_popup.hot_tab_bar.setCurrentIndex(1)

    qtbot.waitUntil(lambda: hotkey_calls == ["dsp", "movie"])
    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["movie-热搜"])

    window._global_search_popup.hot_tab_bar.setCurrentIndex(1)
    qtbot.wait(50)

    assert hotkey_calls == ["dsp", "movie"]


def test_main_window_global_search_popup_shows_source_tabs_and_restores_saved_source(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=["庆余年"], global_search_hot_source="iqiyi"),
        global_search_hotkey_loader=lambda *args: [{"title": "爱奇艺热搜", "query": "爱奇艺热搜"}],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: window._global_search_popup.isVisible() is True)
    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["爱奇艺热搜"])

    assert _popup_hot_source_titles(window) == ["全网", "腾讯", "爱奇艺"]
    assert window._global_search_popup.current_hot_source() == "iqiyi"


def test_main_window_global_search_popup_rebuilds_categories_per_source(qtbot) -> None:
    hotkey_calls: list[tuple[str, str]] = []

    def hotkey_loader(*args) -> list[dict[str, str]]:
        if len(args) == 1:
            hot_type = args[0]
            hotkey_calls.append(("360", hot_type))
            return [{"title": f"360-{hot_type}", "query": f"360-{hot_type}"}]
        source, hot_type = args
        hotkey_calls.append((source, hot_type))
        return [{"title": f"{source}-{hot_type}", "query": f"{source}-{hot_type}"}]

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=["庆余年"]),
        global_search_hotkey_loader=hotkey_loader,
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: _popup_hot_tab_titles(window) == ["综合", "电视剧", "电影", "综艺", "动漫"])
    window._global_search_popup.hot_source_tab_bar.setCurrentIndex(1)

    qtbot.waitUntil(lambda: window._global_search_popup.current_hot_source() == "tencent")
    qtbot.waitUntil(lambda: _popup_hot_tab_titles(window) == ["热搜"])
    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["tencent-hot"])

    assert hotkey_calls[-1] == ("tencent", "hot")


def test_main_window_global_search_popup_switching_source_updates_config_and_uses_source_category_cache(qtbot) -> None:
    saved = {"count": 0}
    hotkey_calls: list[tuple[str, str]] = []

    def hotkey_loader(*args) -> list[dict[str, str]]:
        if len(args) == 1:
            source = "360"
            hot_type = args[0]
        else:
            source, hot_type = args
        hotkey_calls.append((source, hot_type))
        return [{"title": f"{source}-{hot_type}", "query": f"{source}-{hot_type}"}]

    config = AppConfig(global_search_history=["庆余年"])
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
        global_search_hotkey_loader=hotkey_loader,
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["360-dsp"])
    window._global_search_popup.hot_tab_bar.setCurrentIndex(1)
    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["360-movie"])
    window._global_search_popup.hot_source_tab_bar.setCurrentIndex(1)
    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["tencent-hot"])
    window._global_search_popup.hot_source_tab_bar.setCurrentIndex(0)
    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["360-movie"])

    assert hotkey_calls == [("360", "dsp"), ("360", "movie"), ("tencent", "hot")]
    assert config.global_search_hot_source == "360"
    assert saved["count"] == 2


def test_main_window_global_search_popup_iqiyi_dynamic_categories_restore_preferred_tab(qtbot) -> None:
    hotkey_calls: list[tuple[str, str]] = []

    def hotkey_loader(*args):
        if len(args) == 1:
            source = "360"
            hot_type = args[0]
        else:
            source, hot_type = args
        hotkey_calls.append((source, hot_type))
        if source == "iqiyi" and hot_type == "hot":
            return {
                "source": "iqiyi",
                "category": "hot",
                "categories": [("hot", "热搜"), ("movie", "电视剧"), ("tv", "电影")],
                "items": [{"title": "爱奇艺热搜", "query": "爱奇艺热搜"}],
            }
        return [{"title": f"{source}-{hot_type}", "query": f"{source}-{hot_type}"}]

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=["庆余年"]),
        global_search_hotkey_loader=hotkey_loader,
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["360-dsp"])
    window._global_search_popup.hot_tab_bar.setCurrentIndex(1)
    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["360-movie"])
    window._global_search_popup.hot_source_tab_bar.setCurrentIndex(2)

    qtbot.waitUntil(lambda: _popup_hot_tab_titles(window) == ["热搜", "电视剧", "电影"])
    qtbot.waitUntil(lambda: window._global_search_popup.current_hot_tab_type() == "movie")
    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["iqiyi-movie"])

    assert hotkey_calls == [("360", "dsp"), ("360", "movie"), ("iqiyi", "hot"), ("iqiyi", "movie")]


def test_main_window_global_search_popup_iqiyi_many_tabs_use_wider_hot_panel(qtbot) -> None:
    def hotkey_loader(*args):
        if len(args) == 1:
            source = "360"
            hot_type = args[0]
        else:
            source, hot_type = args
        if source == "iqiyi" and hot_type == "hot":
            return {
                "source": "iqiyi",
                "category": "hot",
                "categories": [
                    ("hot", "热搜"),
                    ("movie", "电视剧"),
                    ("tv", "电影"),
                    ("variety", "综艺"),
                    ("comic", "动漫"),
                    ("doc", "纪录片"),
                    ("knowledge", "知识"),
                ],
                "items": [{"title": "爱奇艺热搜", "query": "爱奇艺热搜"}],
            }
        return [{"title": f"{source}-{hot_type}", "query": f"{source}-{hot_type}"}]

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=["庆余年"], global_search_hot_source="iqiyi"),
        global_search_hotkey_loader=hotkey_loader,
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: _popup_hot_tab_titles(window) == ["热搜", "电视剧", "电影", "综艺", "动漫", "纪录片", "知识"])

    assert window._global_search_popup.width() >= 720
    assert window._global_search_popup.hot_tab_bar.elideMode() == Qt.TextElideMode.ElideNone
    assert window._global_search_popup.hot_tab_bar.usesScrollButtons() is True


def test_main_window_global_search_popup_tencent_dynamic_categories_restore_preferred_tab(qtbot) -> None:
    hotkey_calls: list[tuple[str, str]] = []

    def hotkey_loader(*args):
        if len(args) == 1:
            source = "360"
            hot_type = args[0]
        else:
            source, hot_type = args
        hotkey_calls.append((source, hot_type))
        if source == "tencent" and hot_type == "hot":
            return {
                "source": "tencent",
                "category": "hot",
                "categories": [("hot", "热搜"), ("movie", "电视剧")],
                "items": [{"title": "腾讯热搜", "query": "腾讯热搜"}],
            }
        return [{"title": f"{source}-{hot_type}", "query": f"{source}-{hot_type}"}]

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=["庆余年"]),
        global_search_hotkey_loader=hotkey_loader,
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["360-dsp"])
    window._global_search_popup.hot_tab_bar.setCurrentIndex(1)
    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["360-movie"])
    window._global_search_popup.hot_source_tab_bar.setCurrentIndex(1)

    qtbot.waitUntil(lambda: _popup_hot_tab_titles(window) == ["热搜", "电视剧"])
    qtbot.waitUntil(lambda: window._global_search_popup.current_hot_tab_type() == "movie")
    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["tencent-movie"])

    assert hotkey_calls == [("360", "dsp"), ("360", "movie"), ("tencent", "hot"), ("tencent", "movie")]


def test_main_window_global_search_popup_clicking_history_starts_search(qtbot, monkeypatch) -> None:
    started_keywords: list[str] = []
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=["庆余年"]),
        global_search_hotkey_loader=lambda hot_type: [],
        plugin_manager=FakePluginManager(),
    )
    monkeypatch.setattr(window, "_start_global_search", lambda: started_keywords.append(window.global_search_edit.text()))

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: _popup_history_texts(window) == ["庆余年"])
    qtbot.mouseClick(window._global_search_popup.history_item_button("庆余年"), Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: started_keywords == ["庆余年"])
    assert window.global_search_edit.text() == "庆余年"
    assert window._global_search_popup.isVisible() is False


def test_main_window_global_search_popup_clicking_hot_item_starts_search(qtbot, monkeypatch) -> None:
    started_keywords: list[str] = []
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=[]),
        global_search_hotkey_loader=lambda hot_type: [{"title": "热搜一", "query": "热搜一"}],
        plugin_manager=FakePluginManager(),
    )
    monkeypatch.setattr(window, "_start_global_search", lambda: started_keywords.append(window.global_search_edit.text()))

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["热搜一"])
    qtbot.mouseClick(window._global_search_popup.hot_item_button("热搜一"), Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: started_keywords == ["热搜一"])
    assert window.global_search_edit.text() == "热搜一"
    assert window._global_search_popup.isVisible() is False


def test_main_window_global_search_popup_history_actions_update_config(qtbot) -> None:
    saved = {"count": 0}
    config = AppConfig(global_search_history=["庆余年", "琅琊榜"])
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
        global_search_hotkey_loader=lambda hot_type: [],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: _popup_history_texts(window) == ["庆余年", "琅琊榜"])
    qtbot.mouseClick(_popup_delete_button(window, "庆余年"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: config.global_search_history == ["琅琊榜"])
    qtbot.waitUntil(lambda: _popup_history_texts(window) == ["琅琊榜"])
    qtbot.mouseClick(window._global_search_popup.clear_history_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: config.global_search_history == [])

    assert saved["count"] == 2
    assert _popup_history_texts(window) == []


def test_main_window_global_search_popup_history_rows_use_fixed_height(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=["庆余年"]),
        global_search_hotkey_loader=lambda hot_type: [],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: _popup_history_texts(window) == ["庆余年"])
    assert _popup_history_row(window, "庆余年").height() == window._global_search_popup.HISTORY_ITEM_HEIGHT


def test_main_window_global_search_popup_hot_items_show_rank_numbers(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=[]),
        global_search_hotkey_loader=lambda hot_type: [
            {"title": "热搜一", "query": "热搜一"},
            {"title": "热搜二", "query": "热搜二"},
        ],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: _popup_hot_texts(window) == ["热搜一", "热搜二"])
    assert _popup_hot_ranks(window) == ["01", "02"]


def test_main_window_global_search_popup_hides_on_outside_click(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=["庆余年"]),
        global_search_hotkey_loader=lambda hot_type: [],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: _popup_history_texts(window) == ["庆余年"])
    window._handle_global_search_global_mouse_press(window.mapToGlobal(window.rect().bottomRight()) + main_window_module.QPoint(20, 20))

    assert window._global_search_popup.isVisible() is False


def test_main_window_global_search_popup_hides_when_toggling_button(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=["庆余年"]),
        global_search_hotkey_loader=lambda hot_type: [],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: _popup_history_texts(window) == ["庆余年"])
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    assert window._global_search_popup.isVisible() is False


def test_main_window_global_search_records_history_and_deduplicates(qtbot) -> None:
    telegram = SearchableController([])
    config = AppConfig(global_search_history=["庆余年", "琅琊榜"])
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("繁花")
    window.global_search_button.click()
    qtbot.waitUntil(lambda: telegram.search_calls == [("繁花", 1)])

    window._clear_global_search()
    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()
    qtbot.waitUntil(lambda: telegram.search_calls == [("繁花", 1), ("庆余年", 1)])

    assert config.global_search_history == ["庆余年", "繁花", "琅琊榜"]


def test_main_window_global_search_popup_shows_only_latest_ten_history_items(qtbot) -> None:
    history = [f"关键词{i}" for i in range(12, 0, -1)]
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(global_search_history=history),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: window._global_search_popup.isVisible() is True)
    assert _popup_history_texts(window) == history[:10]


def test_main_window_global_search_does_not_record_direct_open_url_history(qtbot, monkeypatch) -> None:
    opened: list[OpenPlayerRequest] = []
    config = AppConfig(global_search_history=["庆余年"])

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        drive_detail_loader=lambda link: {
            "list": [
                {
                    "vod_id": "1$91792$1",
                    "vod_name": "夸克资源",
                    "vod_play_url": "第1集$https://media.example/quark-1.m3u8",
                }
            ]
        },
        plugin_manager=FakePluginManager(),
    )
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "_open_player_immediately", lambda request, restore_paused_state=False: None)

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("https://pan.quark.cn/s/demo")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: len(opened) == 1)
    assert config.global_search_history == ["庆余年"]


def test_main_window_global_search_includes_pansou_when_enabled(qtbot) -> None:
    pansou = SearchableController([_vod("盘搜结果", vod_id="pan-1")], total=1)

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        pansou_controller=pansou,
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == ["盘搜(1)"])
    assert pansou.search_calls == [("庆余年", 1)]


def test_main_window_global_search_treats_drive_url_as_direct_detail_open(qtbot, monkeypatch) -> None:
    telegram = SearchableController([])
    emby = SearchableController([])
    jellyfin = SearchableController([])
    feiniu = SearchableController([])
    drive_calls: list[str] = []
    opened: list[OpenPlayerRequest] = []

    def load_drive_detail(link: str) -> dict:
        drive_calls.append(link)
        return {
            "list": [
                {
                    "vod_id": "1$91792$1",
                    "vod_name": "夸克资源",
                    "vod_play_url": "第1集$https://media.example/quark-1.m3u8#第2集$https://media.example/quark-2.m3u8",
                }
            ]
        }

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=emby,
        jellyfin_controller=jellyfin,
        feiniu_controller=feiniu,
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        drive_detail_loader=load_drive_detail,
        plugin_manager=FakePluginManager(),
    )
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "_open_player_immediately", lambda request, restore_paused_state=False: None)

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("https://pan.quark.cn/s/demo")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: len(opened) == 1)

    assert drive_calls == ["https://pan.quark.cn/s/demo"]
    assert telegram.search_calls == []
    assert emby.search_calls == []
    assert jellyfin.search_calls == []
    assert feiniu.search_calls == []
    assert opened[0].vod.vod_name == "夸克资源"
    assert [item.title for item in opened[0].playlist] == ["第1集", "第2集"]
    assert opened[0].source_vod_id == "https://pan.quark.cn/s/demo"


def test_main_window_global_search_drive_url_opens_placeholder_player_immediately(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.opened: list[tuple[object, bool]] = []
            self.closed_to_main = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.quark.cn/s/demo"
        return {
            "list": [
                {
                    "vod_id": "1$91792$1",
                    "vod_name": "夸克资源",
                    "vod_play_url": "第1集$https://media.example/quark-1.m3u8",
                }
            ]
        }

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    builders: list[object] = []
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        drive_detail_loader=load_drive_detail,
        plugin_manager=FakePluginManager(),
    )
    monkeypatch.setattr(window, "_start_open_request", lambda builder: builders.append(builder) or 7)

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("https://pan.quark.cn/s/demo")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1)
    placeholder_session = window.player_window.opened[0][0]
    assert placeholder_session["is_placeholder"] is True
    assert placeholder_session["vod"].vod_name == "https://pan.quark.cn/s/demo"
    assert placeholder_session["initial_log_message"] == "正在加载详情..."

    assert len(builders) == 1
    real_request = builders[0]()
    assert real_request.vod.vod_name == "夸克资源"
    assert [item.title for item in real_request.playlist] == ["第1集"]


def test_main_window_global_search_treats_non_drive_url_as_direct_parse(qtbot, monkeypatch) -> None:
    class FakeParserService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def resolve(self, flag: str, url: str, preferred_key: str = ""):
            self.calls.append((flag, url, preferred_key))
            return type(
                "Result",
                (),
                {
                    "parser_key": "jx1",
                    "url": "https://media.example/parsed.m3u8",
                    "headers": {"Referer": "https://site.example"},
                },
            )()

    telegram = SearchableController([])
    emby = SearchableController([])
    jellyfin = SearchableController([])
    feiniu = SearchableController([])
    parser_service = FakeParserService()
    opened: list[OpenPlayerRequest] = []

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=emby,
        jellyfin_controller=jellyfin,
        feiniu_controller=feiniu,
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(preferred_parse_key="jx1"),
        plugin_manager=FakePluginManager(),
        playback_parser_service=parser_service,
        direct_parse_detail_loader=lambda _url: {},
    )
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("https://v.qq.com/x/cover/demo.html")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: len(opened) == 1)

    assert telegram.search_calls == []
    assert emby.search_calls == []
    assert jellyfin.search_calls == []
    assert feiniu.search_calls == []
    assert opened[0].source_kind == "direct_parse"
    assert opened[0].source_mode == "parse"
    assert opened[0].source_vod_id == "https://v.qq.com/x/cover/demo.html"
    assert opened[0].use_local_history is False
    assert opened[0].playlist[0].parse_required is True
    assert opened[0].playlist[0].url == ""

    opened[0].playback_loader(opened[0].playlist[0])

    assert parser_service.calls == [("", "https://v.qq.com/x/cover/demo.html", "jx1")]
    assert opened[0].playlist[0].url == "https://media.example/parsed.m3u8"
    assert opened[0].playlist[0].headers == {"Referer": "https://site.example"}


def test_main_window_global_search_treats_youtube_url_as_async_ytdlp_request(qtbot, monkeypatch) -> None:
    class FakeYtdlpService:
        def __init__(self) -> None:
            self.resolve_fast_calls: list[str] = []
            self.resolve_calls: list[str] = []

        def is_available(self) -> bool:
            return True

        def can_resolve(self, url: str) -> bool:
            return "youtube.com" in url

        def playback_format_selector(self, max_height: int | None = 1080) -> str:
            return (
                f"bestvideo[height<={max_height}]+bestaudio/"
                f"best[height<={max_height}]/bestvideo+bestaudio/best"
            )

        def resolve(self, url: str, *, max_height: int | None = None, selected_audio_track_id: str = ""):
            del selected_audio_track_id
            assert max_height is None
            self.resolve_calls.append(url)
            return type(
                "Result",
                (),
                {
                    "url": "https://www.youtube.com/watch?v=test123",
                    "title": "Async Test Video",
                    "thumbnail": "https://img.example/poster.jpg",
                    "description": "async description",
                    "duration_seconds": 321,
                    "headers": {"Referer": "https://www.youtube.com/"},
                    "subtitles": [],
                    "qualities": [],
                    "audio_url": "",
                    "ytdl_format": "299+140",
                    "extractor": "youtube",
                    "selected_quality_id": "ytdlp_1080",
                },
            )()

        def resolve_fast(self, url: str, *, max_height: int | None = None):
            del max_height
            self.resolve_fast_calls.append(url)
            return type(
                "Result",
                (),
                {
                    "url": "https://rr.example/video.mp4",
                    "title": "",
                    "thumbnail": "",
                    "description": "",
                    "duration_seconds": 0,
                    "headers": {},
                    "subtitles": [],
                    "qualities": [],
                    "audio_url": "https://rr.example/audio.webm",
                    "audio_tracks": [],
                    "selected_audio_track_id": "",
                    "ytdl_format": "",
                    "extractor": "",
                    "selected_quality_id": "ytdlp_1080",
                    "detail_fields": [],
                },
            )()

        def resolve_for_quality(self, url: str, quality_id: str, *, audio_track_id: str = ""):
            del audio_track_id
            return self.resolve(url)

        def resolve_to_play_item(self, url: str):
            raise AssertionError("resolve_to_play_item should not be used")

        def apply_result(self, result, *, vod=None, item=None, source_url: str = "") -> None:
            resolved_title = result.title or (item.title if item is not None else source_url)
            if vod is not None:
                if result.title:
                    vod.vod_name = resolved_title
                if result.thumbnail:
                    vod.vod_pic = result.thumbnail
                if result.description:
                    vod.vod_content = result.description
            if item is None:
                return
            item.url = result.url
            item.original_url = source_url
            item.headers = dict(result.headers)
            item.audio_url = result.audio_url
            item.ytdl_format = result.ytdl_format
            item.playback_qualities = list(result.qualities)
            item.external_subtitles = list(result.subtitles)
            item.duration_seconds = result.duration_seconds
            item.title = resolved_title
            item.media_title = resolved_title
            item.selected_playback_quality_id = result.selected_quality_id

    opened: list[OpenPlayerRequest] = []
    errors: list[str] = []
    service = FakeYtdlpService()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
        yt_dlp_service=service,
    )
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "show_error", errors.append)

    qtbot.addWidget(window)
    window.show()

    url = "https://www.youtube.com/watch?v=test123"
    window.global_search_edit.setText(url)
    window.global_search_button.click()

    qtbot.waitUntil(lambda: len(opened) == 1 or len(errors) == 1)

    assert errors == []
    request = opened[0]
    assert request.source_kind == "direct_parse"
    assert request.source_mode == "ytdlp"
    assert request.source_vod_id == url
    assert request.async_playback_loader is True
    assert request.playlist[0].url == url
    assert request.playlist[0].original_url == url
    assert request.playlist[0].ytdl_format == "bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best"
    assert request.playlist[0].selected_playback_quality_id == ""

    session = type("Session", (), {"vod": request.vod})()
    request.playback_loader(session, request.playlist[0])

    assert service.resolve_fast_calls == [url]
    assert service.resolve_calls == []
    assert session.vod.vod_name == url
    assert session.vod.vod_pic == ""
    assert session.vod.vod_content == ""
    assert request.playlist[0].url == "https://rr.example/video.mp4"
    assert request.playlist[0].audio_url == "https://rr.example/audio.webm"
    assert request.playlist[0].headers == {}
    assert request.playlist[0].selected_playback_quality_id == "ytdlp_1080"
    assert request.playlist[0].ytdl_format == ""


def test_main_window_ytdlp_loader_resolves_selected_quality_on_reload(qtbot) -> None:
    class FakeYtdlpService:
        def __init__(self) -> None:
            self.resolve_calls: list[str] = []
            self.resolve_for_quality_calls: list[tuple[str, str]] = []

        def is_available(self) -> bool:
            return True

        def can_resolve(self, url: str) -> bool:
            return "youtube.com" in url

        def playback_format_selector(self, max_height: int | None = None) -> str:
            return (
                f"bestvideo[height<={max_height}]+bestaudio/"
                f"best[height<={max_height}]/bestvideo+bestaudio/best"
            )

        def resolve(self, url: str, *, max_height: int | None = None, selected_audio_track_id: str = ""):
            del selected_audio_track_id
            assert max_height is None
            self.resolve_calls.append(url)
            return type(
                "Result",
                (),
                {
                    "url": "https://www.youtube.com/watch?v=test123",
                    "title": "Async Test Video",
                    "thumbnail": "",
                    "description": "",
                    "duration_seconds": 321,
                    "headers": {},
                    "subtitles": [],
                    "qualities": [],
                    "audio_url": "",
                    "ytdl_format": "299+140",
                    "extractor": "youtube",
                    "selected_quality_id": "ytdlp_1080",
                },
            )()

        def resolve_for_quality(self, url: str, quality_id: str, *, audio_track_id: str = ""):
            del audio_track_id
            self.resolve_for_quality_calls.append((url, quality_id))
            return type(
                "Result",
                (),
                {
                    "url": "https://www.youtube.com/watch?v=test123",
                    "title": "Async Test Video",
                    "thumbnail": "",
                    "description": "",
                    "duration_seconds": 321,
                    "headers": {},
                    "subtitles": [],
                    "qualities": [],
                    "audio_url": "",
                    "ytdl_format": "298+140",
                    "extractor": "youtube",
                    "selected_quality_id": quality_id,
                },
            )()

        def apply_result(self, result, *, vod=None, item=None, source_url: str = "") -> None:
            resolved_title = result.title or source_url
            if vod is not None:
                vod.vod_name = resolved_title
                vod.vod_pic = result.thumbnail
                vod.vod_content = result.description
            if item is None:
                return
            item.url = result.url
            item.original_url = source_url
            item.headers = dict(result.headers)
            item.audio_url = result.audio_url
            item.ytdl_format = result.ytdl_format
            item.playback_qualities = list(result.qualities)
            item.external_subtitles = list(result.subtitles)
            item.duration_seconds = result.duration_seconds
            item.title = resolved_title
            item.media_title = resolved_title
            item.selected_playback_quality_id = result.selected_quality_id

    service = FakeYtdlpService()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
        yt_dlp_service=service,
    )

    qtbot.addWidget(window)

    request = window._build_ytdlp_parse_request("https://www.youtube.com/watch?v=test123")
    session = type("Session", (), {"vod": request.vod})()
    item = request.playlist[0]
    item.selected_playback_quality_id = "ytdlp_720"

    request.playback_loader(session, item)

    assert service.resolve_calls == []
    assert service.resolve_for_quality_calls == [("https://www.youtube.com/watch?v=test123", "ytdlp_720")]
    assert item.url == "https://www.youtube.com/watch?v=test123"
    assert item.audio_url == ""
    assert item.selected_playback_quality_id == "ytdlp_720"
    assert item.ytdl_format == "298+140"


def test_main_window_ytdlp_loader_passes_selected_audio_track_id(qtbot) -> None:
    class FakeYtdlpService:
        def __init__(self) -> None:
            self.resolve_calls: list[tuple[str, int | None, str]] = []
            self.resolve_for_quality_calls: list[tuple[str, str, str]] = []

        def is_available(self) -> bool:
            return True

        def can_resolve(self, url: str) -> bool:
            return "youtube.com" in url

        def resolve(self, url: str, *, max_height: int | None = None, selected_audio_track_id: str = ""):
            self.resolve_calls.append((url, max_height, selected_audio_track_id))
            raise AssertionError("resolve() should not be used when a ytdlp quality is selected")

        def resolve_for_quality(self, url: str, quality_id: str, *, audio_track_id: str = ""):
            self.resolve_for_quality_calls.append((url, quality_id, audio_track_id))
            return type(
                "Result",
                (),
                {
                    "url": "https://www.youtube.com/watch?v=test123",
                    "title": "Async Test Video",
                    "thumbnail": "",
                    "description": "",
                    "duration_seconds": 321,
                    "headers": {},
                    "subtitles": [],
                    "qualities": [],
                    "audio_url": "",
                    "audio_tracks": [],
                    "selected_audio_track_id": audio_track_id,
                    "ytdl_format": "298+140-dub",
                    "extractor": "youtube",
                    "selected_quality_id": quality_id,
                },
            )()

        def apply_result(self, result, *, vod=None, item=None, source_url: str = "") -> None:
            resolved_title = result.title or source_url
            if vod is not None:
                vod.vod_name = resolved_title
                vod.vod_pic = result.thumbnail
                vod.vod_content = result.description
            if item is None:
                return
            item.url = result.url
            item.original_url = source_url
            item.headers = dict(result.headers)
            item.audio_url = result.audio_url
            item.audio_tracks = list(result.audio_tracks)
            item.selected_audio_track_id = result.selected_audio_track_id
            item.ytdl_format = result.ytdl_format
            item.playback_qualities = list(result.qualities)
            item.external_subtitles = list(result.subtitles)
            item.duration_seconds = result.duration_seconds
            item.title = resolved_title
            item.media_title = resolved_title
            item.selected_playback_quality_id = result.selected_quality_id

    service = FakeYtdlpService()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
        yt_dlp_service=service,
    )

    qtbot.addWidget(window)

    request = window._build_ytdlp_parse_request("https://www.youtube.com/watch?v=test123")
    session = type("Session", (), {"vod": request.vod})()
    item = request.playlist[0]
    item.selected_playback_quality_id = "ytdlp_720"
    item.selected_audio_track_id = "ytdlp_audio_zh_140-dub"

    request.playback_loader(session, item)

    assert service.resolve_calls == []
    assert service.resolve_for_quality_calls == [
        ("https://www.youtube.com/watch?v=test123", "ytdlp_720", "ytdlp_audio_zh_140-dub")
    ]
    assert item.selected_audio_track_id == "ytdlp_audio_zh_140-dub"
    assert item.ytdl_format == "298+140-dub"


def test_main_window_direct_parse_fallback_to_ytdlp_overwrites_session_metadata(qtbot) -> None:
    class FailingParserService:
        def resolve(self, flag: str, url: str, preferred_key: str = ""):
            raise ValueError("parser failed")

    class FakeYtdlpService:
        def is_available(self) -> bool:
            return True

        def resolve(self, url: str, *, max_height: int | None = None, selected_audio_track_id: str = ""):
            del selected_audio_track_id
            return type(
                "Result",
                (),
                {
                    "url": "https://www.youtube.com/watch?v=test123",
                    "title": "Fallback Video",
                    "thumbnail": "https://img.example/fallback.jpg",
                    "description": "fallback description",
                    "duration_seconds": 654,
                    "headers": {"Referer": "https://www.youtube.com/"},
                    "subtitles": [],
                    "qualities": [],
                    "audio_url": "",
                    "ytdl_format": "299+140",
                    "extractor": "youtube",
                    "selected_quality_id": "ytdlp_1080",
                },
            )()

        def resolve_for_quality(self, url: str, quality_id: str, *, audio_track_id: str = ""):
            del audio_track_id
            return self.resolve(url)

        def apply_result(self, result, *, vod=None, item=None, source_url: str = "") -> None:
            resolved_title = result.title or source_url
            if vod is not None:
                vod.vod_name = resolved_title
                vod.vod_pic = result.thumbnail
                vod.vod_content = result.description
            if item is None:
                return
            item.url = result.url
            item.original_url = source_url
            item.headers = dict(result.headers)
            item.audio_url = result.audio_url
            item.ytdl_format = result.ytdl_format
            item.playback_qualities = list(result.qualities)
            item.external_subtitles = list(result.subtitles)
            item.duration_seconds = result.duration_seconds
            item.title = resolved_title
            item.media_title = resolved_title
            item.selected_playback_quality_id = result.selected_quality_id

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(preferred_parse_key="jx1"),
        plugin_manager=FakePluginManager(),
        playback_parser_service=FailingParserService(),
        yt_dlp_service=FakeYtdlpService(),
    )

    qtbot.addWidget(window)

    request = window._build_direct_parse_request("https://www.youtube.com/watch?v=test123")
    session = type("Session", (), {"vod": request.vod})()
    item = request.playlist[0]

    request.playback_loader(session, item)

    assert session.vod.vod_name == "Fallback Video"
    assert session.vod.vod_pic == "https://img.example/fallback.jpg"
    assert session.vod.vod_content == "fallback description"
    assert item.url == "https://www.youtube.com/watch?v=test123"
    assert item.headers == {"Referer": "https://www.youtube.com/"}
    assert item.selected_playback_quality_id == "ytdlp_1080"
    assert item.ytdl_format == "299+140"


def test_main_window_ytdlp_request_attaches_history_restore_hooks(qtbot) -> None:
    history_calls: list[str] = []
    saved_calls: list[tuple[str, dict[str, object]]] = []

    class FakeYtdlpService:
        def is_available(self) -> bool:
            return True

        def can_resolve(self, url: str) -> bool:
            return "youtube.com" in url

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
        yt_dlp_service=FakeYtdlpService(),
        direct_parse_playback_history_loader=lambda vod_id: history_calls.append(vod_id),
        direct_parse_playback_history_saver=lambda vod_id, payload: saved_calls.append((vod_id, payload)),
    )
    qtbot.addWidget(window)

    request = window._build_ytdlp_parse_request("https://www.youtube.com/watch?v=test123")

    assert request.playback_history_loader is not None
    assert request.playback_history_saver is not None
    request.playback_history_loader()
    request.playback_history_saver({"position": 12})
    assert history_calls == ["https://www.youtube.com/watch?v=test123"]
    assert saved_calls == [("https://www.youtube.com/watch?v=test123", {"position": 12})]


def test_main_window_restore_request_routes_youtube_parse_urls_to_ytdlp(qtbot) -> None:
    class FakeYtdlpService:
        def is_available(self) -> bool:
            return True

        def can_resolve(self, url: str) -> bool:
            return "youtube.com" in url

    parser_service = object()

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(
            last_playback_source="direct_parse",
            last_playback_mode="parse",
            last_playback_vod_id="https://www.youtube.com/watch?v=test123",
        ),
        plugin_manager=FakePluginManager(),
        playback_parser_service=parser_service,
        yt_dlp_service=FakeYtdlpService(),
        direct_parse_playback_history_loader=lambda vod_id: HistoryRecord(
            id=1,
            key=vod_id,
            vod_name="saved",
            vod_pic="",
            vod_remarks="",
            episode=0,
            episode_url=vod_id,
            position=156000,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
        ),
    )
    qtbot.addWidget(window)

    request = window._build_restore_request()

    assert request is not None
    assert request.source_mode == "ytdlp"
    assert request.playback_history_loader is not None
    assert request.playback_history_loader().position == 156000


def test_main_window_restore_request_routes_saved_ytdlp_mode_urls_to_ytdlp(qtbot) -> None:
    class FakeYtdlpService:
        def is_available(self) -> bool:
            return True

        def can_resolve(self, url: str) -> bool:
            return "youtube.com" in url

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(
            last_playback_source="direct_parse",
            last_playback_mode="ytdlp",
            last_playback_vod_id="https://www.youtube.com/watch?v=test123",
        ),
        plugin_manager=FakePluginManager(),
        yt_dlp_service=FakeYtdlpService(),
        direct_parse_playback_history_loader=lambda vod_id: HistoryRecord(
            id=1,
            key=vod_id,
            vod_name="saved",
            vod_pic="",
            vod_remarks="",
            episode=0,
            episode_url=vod_id,
            position=156000,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
        ),
    )
    qtbot.addWidget(window)

    request = window._build_restore_request()

    assert request is not None
    assert request.source_mode == "ytdlp"
    assert request.playback_history_loader is not None
    assert request.playback_history_loader().position == 156000


def test_main_window_history_detail_routes_youtube_parse_urls_to_ytdlp(qtbot, monkeypatch) -> None:
    class FakeYtdlpService:
        def is_available(self) -> bool:
            return True

        def can_resolve(self, url: str) -> bool:
            return "youtube.com" in url

    opened: list[OpenPlayerRequest] = []
    parser_service = object()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
        playback_parser_service=parser_service,
        yt_dlp_service=FakeYtdlpService(),
        direct_parse_playback_history_loader=lambda vod_id: HistoryRecord(
            id=1,
            key=vod_id,
            vod_name="saved",
            vod_pic="",
            vod_remarks="",
            episode=0,
            episode_url=vod_id,
            position=156000,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
        ),
    )
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    qtbot.addWidget(window)
    window.show()

    window.open_history_detail(
        HistoryRecord(
            id=1,
            key="https://www.youtube.com/watch?v=test123",
            vod_name="saved",
            vod_pic="",
            vod_remarks="",
            source_kind="direct_parse",
            episode=0,
            episode_url="https://www.youtube.com/watch?v=test123",
            position=156000,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
        )
    )

    qtbot.waitUntil(lambda: len(opened) == 1)
    assert opened[0].source_mode == "ytdlp"
    assert opened[0].playback_history_loader is not None
    assert opened[0].playback_history_loader().position == 156000


@pytest.mark.parametrize(
    "link",
    [
        "magnet:?xt=urn:btih:8a06396e03acb19d72eb2d779a22b2dc00f66a33",
        "ed2k://|file|demo.mp4|123456|ABCDEF|/",
    ],
)
def test_main_window_global_search_treats_magnet_and_ed2k_as_offline_download(qtbot, monkeypatch, link: str) -> None:
    telegram = SearchableController([])
    emby = SearchableController([])
    jellyfin = SearchableController([])
    feiniu = SearchableController([])
    offline_calls: list[str] = []
    opened: list[OpenPlayerRequest] = []

    def load_offline_detail(link: str) -> dict:
        offline_calls.append(link)
        return {
            "list": [
                {
                    "vod_id": "1$107919$1",
                    "vod_name": "离线下载结果",
                    "vod_play_from": "丫仙女",
                    "vod_play_url": "离线文件.mp4(6.11 GB)$1@107920@0@0",
                    "path": "/我的115云盘/alist-tvbox-offline/离线文件/~playlist",
                }
            ]
        }

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=emby,
        jellyfin_controller=jellyfin,
        feiniu_controller=feiniu,
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
        offline_download_detail_loader=load_offline_detail,
    )
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "_open_player_immediately", lambda request, restore_paused_state=False: None)

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText(link)
    window.global_search_button.click()

    qtbot.waitUntil(lambda: len(opened) == 1)

    assert offline_calls == [link]
    assert telegram.search_calls == []
    assert emby.search_calls == []
    assert jellyfin.search_calls == []
    assert feiniu.search_calls == []
    assert opened[0].vod.vod_name == "离线下载结果"
    assert [item.title for item in opened[0].playlist] == ["离线文件.mp4(6.11 GB)"]
    assert opened[0].source_vod_id == link


def test_main_window_global_search_offline_download_opens_placeholder_player_immediately(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.opened: list[tuple[object, bool]] = []
            self.closed_to_main = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    def load_offline_detail(link: str) -> dict:
        assert link == "magnet:?xt=urn:btih:8a06396e03acb19d72eb2d779a22b2dc00f66a33"
        return {
            "list": [
                {
                    "vod_id": "1$107919$1",
                    "vod_name": "离线下载结果",
                    "vod_play_from": "丫仙女",
                    "vod_play_url": "离线文件.mp4(6.11 GB)$1@107920@0@0",
                    "path": "/我的115云盘/alist-tvbox-offline/离线文件/~playlist",
                }
            ]
        }

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    builders: list[object] = []
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
        offline_download_detail_loader=load_offline_detail,
    )
    monkeypatch.setattr(window, "_start_open_request", lambda builder: builders.append(builder) or 7)

    qtbot.addWidget(window)
    window.show()

    magnet = "magnet:?xt=urn:btih:8a06396e03acb19d72eb2d779a22b2dc00f66a33"
    window.global_search_edit.setText(magnet)
    window.global_search_button.click()

    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1)
    placeholder_session = window.player_window.opened[0][0]
    assert placeholder_session["is_placeholder"] is True
    assert placeholder_session["vod"].vod_name == magnet
    assert placeholder_session["initial_log_message"] == "正在加载详情..."

    assert len(builders) == 1
    real_request = builders[0]()
    assert real_request.vod.vod_name == "离线下载结果"
    assert [item.title for item in real_request.playlist] == ["离线文件.mp4(6.11 GB)"]
    assert real_request.source_vod_id == magnet


def test_main_window_global_search_builds_episode_playlist_from_direct_parse_detail(qtbot, monkeypatch) -> None:
    class FakeParserService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def resolve(self, flag: str, url: str, preferred_key: str = ""):
            self.calls.append((flag, url, preferred_key))
            return type(
                "Result",
                (),
                {
                    "parser_key": "jx1",
                    "url": f"https://media.example/{url.rsplit('/', 1)[-1]}.m3u8",
                    "headers": {"Referer": "https://site.example"},
                },
            )()

    def load_detail(url: str) -> dict:
        assert url == "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html"
        return {
            "vod_code": 200,
            "vod_type": "动漫",
            "vod_title": "剑来 第二季",
            "vod_year": "2025",
            "vod_updateTo": "VIP · 全27集 · 21621",
            "vod_pic": "https://image.example/poster.jpg",
            "vod_form": "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html",
            "vod_desc": "desc",
            "vod_episodes": [
                {
                    "name": "第09话",
                    "url": "https://v.qq.com/x/cover/mzc00200xxpsogl/y4101fhe180.html",
                },
                {
                    "name": "第10话",
                    "url": "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html",
                },
                {
                    "name": "第11话",
                    "url": "https://v.qq.com/x/cover/mzc00200xxpsogl/a4101bma2qf.html",
                },
            ],
        }

    parser_service = FakeParserService()
    danmaku_calls: list[str] = []

    def load_danmaku(url: str) -> dict:
        danmaku_calls.append(url)
        return {
            "code": 23,
            "name": "demo",
            "danmuku": [
                [42.741, "right", "#00CD00", "1205421", "666", "03-15 15:47", "25px"],
            ],
        }

    opened: list[OpenPlayerRequest] = []

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(preferred_parse_key="jx1"),
        plugin_manager=FakePluginManager(),
        playback_parser_service=parser_service,
        direct_parse_detail_loader=load_detail,
        direct_parse_danmaku_loader=load_danmaku,
    )
    monkeypatch.setattr(direct_parse_danmaku_module, "load_cached_danmaku_xml", lambda name, reg_src: "")
    monkeypatch.setattr(direct_parse_danmaku_module, "save_cached_danmaku_xml", lambda name, reg_src, xml_text: None)
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html")
    window.global_search_button.click()

    qtbot.waitUntil(lambda: len(opened) == 1)

    assert opened[0].vod.vod_name == "剑来 第二季"
    assert opened[0].source_kind == "direct_parse"
    assert opened[0].vod.type_name == "动漫"
    assert opened[0].vod.vod_year == "2025"
    assert opened[0].vod.vod_remarks == "VIP · 全27集 · 21621"
    assert [item.title for item in opened[0].playlist] == ["第09话", "第10话", "第11话"]
    assert opened[0].clicked_index == 1
    assert opened[0].use_local_history is False
    assert opened[0].danmaku_controller is not None
    assert all(item.parse_required for item in opened[0].playlist)
    assert opened[0].playlist[1].original_url == "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html"
    assert opened[0].playlist[2].url == ""

    opened[0].playback_loader(opened[0].playlist[2])

    assert parser_service.calls == [
        ("", "https://v.qq.com/x/cover/mzc00200xxpsogl/a4101bma2qf.html", "jx1")
    ]
    assert opened[0].playlist[2].url == "https://media.example/a4101bma2qf.html.m3u8"
    assert opened[0].playlist[2].headers == {"Referer": "https://site.example"}
    qtbot.waitUntil(lambda: bool(opened[0].playlist[2].danmaku_xml), timeout=1000)
    assert danmaku_calls == ["https://v.qq.com/x/cover/mzc00200xxpsogl/a4101bma2qf.html"]
    assert "666" in opened[0].playlist[2].danmaku_xml


def test_main_window_opening_pansou_result_restores_browse_tab_and_path(qtbot, monkeypatch) -> None:
    pansou = SearchableResolveController([_vod("盘搜结果", vod_id="pan-1")], resolved_path="/Movies/Resolved", total=1)

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        pansou_controller=pansou,
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )

    shown_paths: list[str] = []
    monkeypatch.setattr(window, "show_browse_path", lambda path: shown_paths.append(path))

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()
    qtbot.waitUntil(lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == ["盘搜(1)"])

    item = _vod("盘搜结果", vod_id="pan-1")
    window._handle_pansou_item_open_requested(item)

    qtbot.waitUntil(lambda: shown_paths == ["/Movies/Resolved"])
    assert pansou.resolve_calls == ["pan-1"]
    assert "文件浏览" in [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())]


def test_main_window_ignores_stale_global_search_results(qtbot) -> None:
    telegram = AsyncKeywordSearchController(
        {
            "旧关键词": ([_vod("旧结果")], 1),
            "新关键词": ([], 0),
        }
    )
    emby = KeywordSearchableController(
        {
            "旧关键词": ([], 0),
            "新关键词": ([_vod("新结果")], 1),
        }
    )

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=emby,
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    window.global_search_edit.setText("旧关键词")
    window.global_search_button.click()
    qtbot.waitUntil(lambda: telegram.search_calls == [("旧关键词", 1)])

    window.global_search_edit.setText("新关键词")
    window.global_search_button.click()
    qtbot.waitUntil(lambda: telegram.search_calls == [("旧关键词", 1), ("新关键词", 1)])

    telegram.release("新关键词")
    qtbot.waitUntil(lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == ["Emby(1)"])

    telegram.release("旧关键词")
    qtbot.wait(100)

    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == ["Emby(1)"]
    assert emby.search_calls == [("旧关键词", 1), ("新关键词", 1)]


def test_main_window_clear_global_search_restores_original_tabs_and_titles(qtbot) -> None:
    telegram = SearchableController([_vod("Telegram One")], total=4)
    plugin_controller = SearchableController([_vod("Plugin One")], total=1)

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"id": "plugin-a", "title": "红果短剧", "controller": plugin_controller, "search_enabled": False},
        ],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.resize(920, 520)
    window.show()

    original_titles = [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())]

    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()
    qtbot.waitUntil(
        lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == [
            "电报影视(4)",
            "红果短剧(1)",
        ]
    )

    window.global_search_clear_button.click()
    qtbot.waitUntil(lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == original_titles)


def test_main_window_clearing_search_text_during_in_progress_search_restores_main_tabs(qtbot) -> None:
    telegram = AsyncKeywordSearchController({"庆余年": ([_vod("Telegram One")], 12)})
    plugin_controller = AsyncKeywordSearchController({"庆余年": ([_vod("Plugin One")], 1)})

    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=telegram,
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"id": "plugin-a", "title": "红果短剧", "controller": plugin_controller, "search_enabled": False},
        ],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    original_titles = [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())]

    window.global_search_edit.setText("庆余年")
    window.global_search_button.click()
    qtbot.waitUntil(lambda: telegram.search_calls == [("庆余年", 1)])
    assert window.nav_tabs.count() == 0

    window.global_search_edit.clear()

    qtbot.waitUntil(lambda: [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == original_titles)
    assert window.global_search_status_label.text() == ""

    telegram.release("庆余年")
    plugin_controller.release("庆余年")
    qtbot.wait(100)

    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == original_titles


def test_main_window_clearing_search_ignores_deleted_active_plugin_page(qtbot) -> None:
    manager = WidthAwarePluginManager()
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=SearchableController([]),
        live_controller=FakeStaticController(),
        emby_controller=SearchableController([]),
        jellyfin_controller=SearchableController([]),
        feiniu_controller=SearchableController([]),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=manager.load_plugins(["1"]),
        plugin_manager=manager,
    )

    qtbot.addWidget(window)
    window.show()
    stale_plugin_page = window._plugin_pages[0][0]
    window.nav_tabs.setCurrentWidget(stale_plugin_page)
    window._active_widget = stale_plugin_page
    window._plugin_definitions = []
    window._plugin_pages = []
    window._plugin_tab_definitions = []
    window._global_search_active = True
    window._global_search_keyword = "庆余年"
    window.global_search_edit.setText("庆余年")
    shiboken6.delete(stale_plugin_page)

    window._clear_global_search()

    assert window.nav_tabs.currentWidget() is window.douban_page


def test_main_window_shows_live_source_manager_button_after_plugin_manager(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        live_source_manager=object(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.plugin_manager_button.toolTip() == "插件管理"
    assert window.live_source_manager_button.toolTip() == "直播源管理"


def test_main_window_keeps_existing_header_buttons_without_parse_manager(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.plugin_manager_button.toolTip() == "插件管理"
    assert window.live_source_manager_button.toolTip() == "直播源管理"
    assert not hasattr(window, "parse_manager_button")


def test_main_window_shows_advanced_settings_button_after_live_source_manager(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        live_source_manager=object(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.live_source_manager_button.toolTip() == "直播源管理"
    assert window.advanced_settings_button.toolTip() == "高级设置"
    assert window.header_layout.indexOf(window.live_source_manager_button) < window.header_layout.indexOf(window.advanced_settings_button)


def test_main_window_opens_advanced_settings_dialog(qtbot, monkeypatch) -> None:
    opened: list[tuple[object, object, object, object, object]] = []

    class FakeDialog:
        def __init__(
            self,
            config,
            save_config,
            parent=None,
            apply_theme=None,
            app_log_service=None,
            youtube_category_text_loader=None,
        ) -> None:
            del youtube_category_text_loader
            opened.append((config, save_config, parent, apply_theme, app_log_service))

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(main_window_module, "AdvancedSettingsDialog", FakeDialog)
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    window._open_advanced_settings()

    assert len(opened) == 1
    assert opened[0][0] is window.config
    assert opened[0][2] is window


def test_main_window_passes_log_service_to_advanced_settings_dialog(qtbot, monkeypatch) -> None:
    opened: list[object] = []
    log_service = object()

    class FakeDialog:
        def __init__(
            self,
            config,
            save_config,
            parent=None,
            apply_theme=None,
            app_log_service=None,
            youtube_category_text_loader=None,
        ) -> None:
            del config, save_config, parent, apply_theme, youtube_category_text_loader
            opened.append(app_log_service)

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(main_window_module, "AdvancedSettingsDialog", FakeDialog)
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        app_log_service=log_service,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    window._open_advanced_settings()

    assert opened == [log_service]


def test_main_window_refreshes_local_telegram_categories_after_advanced_settings(qtbot, monkeypatch) -> None:
    opened: list[object] = []
    backend_telegram = FakeStaticController()
    local_telegram = FakeStaticController()

    class FakeDialog:
        def __init__(
            self,
            config,
            save_config,
            parent=None,
            apply_theme=None,
            app_log_service=None,
            youtube_category_text_loader=None,
            telegram_controller=None,
        ) -> None:
            del config, save_config, parent, apply_theme, app_log_service, youtube_category_text_loader
            opened.append(telegram_controller)

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(main_window_module, "AdvancedSettingsDialog", FakeDialog)
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=backend_telegram,
        telegram_channels_controller=local_telegram,
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    reloads: list[bool] = []
    window.telegram_channels_page.reload_categories = lambda: reloads.append(True)

    window._open_advanced_settings()

    assert opened == [local_telegram]
    assert reloads == [True]


def test_main_window_advanced_settings_save_updates_shared_config(qtbot, monkeypatch) -> None:
    config = AppConfig()
    saved: list[tuple[bool, str, str]] = []

    class FakeDialog:
        def __init__(
            self,
            config_arg,
            save_config,
            parent=None,
            apply_theme=None,
            app_log_service=None,
            youtube_category_text_loader=None,
        ) -> None:
            del parent, apply_theme, app_log_service, youtube_category_text_loader
            config_arg.metadata_enhancement_enabled = False
            config_arg.metadata_douban_cookie = "bid=demo;"
            config_arg.metadata_tmdb_api_key = "tmdb-key"
            save_config()

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(main_window_module, "AdvancedSettingsDialog", FakeDialog)
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: saved.append(
            (
                config.metadata_enhancement_enabled,
                config.metadata_douban_cookie,
                config.metadata_tmdb_api_key,
            )
        ),
    )
    qtbot.addWidget(window)

    window._open_advanced_settings()

    assert saved == [(False, "bid=demo;", "tmdb-key")]


def test_main_window_advanced_settings_refreshes_player_hwdec(qtbot, monkeypatch) -> None:
    class FakePlayerWindow:
        def __init__(self) -> None:
            self.refreshed = 0
            self.session = object()

        def refresh_runtime_video_output_settings(self) -> None:
            self.refreshed += 1

    class FakeDialog:
        def __init__(
            self,
            config_arg,
            save_config,
            parent=None,
            apply_theme=None,
            app_log_service=None,
            youtube_category_text_loader=None,
        ) -> None:
            del parent, apply_theme, app_log_service, youtube_category_text_loader
            config_arg.mpv_hwdec_mode = "auto-copy"
            save_config()

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(main_window_module, "AdvancedSettingsDialog", FakeDialog)
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window.player_window = FakePlayerWindow()

    window._open_advanced_settings()

    assert window.player_window.refreshed == 1


def test_main_window_apply_open_player_supports_legacy_player_window_constructor(qtbot, monkeypatch) -> None:
    opened: list[tuple[object, bool]] = []

    class LegacyPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.controller = controller
            self.config = config
            self.save_config = save_config

        def open_session(self, session, start_paused: bool = False) -> None:
            opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    monkeypatch.setattr(main_window_module, "PlayerWindow", LegacyPlayerWindow)
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        app_log_service=object(),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    request = OpenPlayerRequest(
        vod=VodItem(vod_id="vod-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="https://media.example/1.m3u8")],
        clicked_index=0,
        source_mode="detail",
        source_vod_id="vod-1",
    )

    window._apply_open_player(request, {"session": "ok"})

    assert opened == [({"session": "ok"}, False)]


def test_advanced_settings_dialog_populates_existing_config(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig(
        metadata_enhancement_enabled=False,
        metadata_douban_cookie="bid=demo;",
        metadata_tmdb_api_key="tmdb-demo-key",
        metadata_tmdb_proxy_base_url="https://tmdb.example.com",
        metadata_bangumi_access_token="bgm-demo-token",
    )
    dialog = AdvancedSettingsDialog(config, save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.metadata_enabled_checkbox.isChecked() is False
    assert dialog.douban_cookie_edit.toPlainText() == "bid=demo;"
    assert dialog.tmdb_api_key_edit.text() == "tmdb-demo-key"
    assert dialog.tmdb_endpoint_combo.currentData() == "__custom__"
    assert dialog.tmdb_proxy_base_url_edit.text() == "https://tmdb.example.com"
    assert dialog.bangumi_access_token_edit.text() == "bgm-demo-token"
    assert dialog.douban_cookie_edit.isEnabled() is False
    assert dialog.tmdb_api_key_edit.isEnabled() is False
    assert dialog.tmdb_endpoint_combo.isEnabled() is False
    assert dialog.tmdb_speed_test_button.isEnabled() is False
    assert dialog.tmdb_proxy_base_url_edit.isEnabled() is False
    assert dialog.bangumi_access_token_edit.isEnabled() is False
    assert dialog.douban_cookie_edit.placeholderText() == "填写豆瓣 Cookie；留空时跳过豆瓣官方抓取"


def test_advanced_settings_dialog_toggles_input_enabled_state(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.douban_cookie_edit.isEnabled() is True
    assert dialog.tmdb_api_key_edit.isEnabled() is True
    assert dialog.tmdb_endpoint_combo.isEnabled() is True
    assert dialog.tmdb_speed_test_button.isEnabled() is True
    assert dialog.tmdb_proxy_base_url_edit.isEnabled() is False
    assert dialog.bangumi_access_token_edit.isEnabled() is True

    dialog.tmdb_endpoint_combo.setCurrentIndex(dialog.tmdb_endpoint_combo.findData("__custom__"))
    assert dialog.tmdb_proxy_base_url_edit.isEnabled() is True

    dialog.metadata_enabled_checkbox.setChecked(False)

    assert dialog.douban_cookie_edit.isEnabled() is False
    assert dialog.tmdb_api_key_edit.isEnabled() is False
    assert dialog.tmdb_endpoint_combo.isEnabled() is False
    assert dialog.tmdb_speed_test_button.isEnabled() is False
    assert dialog.tmdb_proxy_base_url_edit.isEnabled() is False
    assert dialog.bangumi_access_token_edit.isEnabled() is False

    dialog.metadata_enabled_checkbox.setChecked(True)

    assert dialog.douban_cookie_edit.isEnabled() is True
    assert dialog.tmdb_api_key_edit.isEnabled() is True
    assert dialog.tmdb_endpoint_combo.isEnabled() is True
    assert dialog.tmdb_speed_test_button.isEnabled() is True
    assert dialog.tmdb_proxy_base_url_edit.isEnabled() is True
    assert dialog.bangumi_access_token_edit.isEnabled() is True


def test_advanced_settings_dialog_saves_trimmed_values(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved: list[AppConfig] = []
    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: saved.append(config))
    qtbot.addWidget(dialog)

    dialog.metadata_enabled_checkbox.setChecked(False)
    dialog.douban_cookie_edit.setPlainText(" bid=demo; ll=118282 \n")
    dialog.tmdb_api_key_edit.setText(" tmdb-demo-key ")
    dialog.tmdb_endpoint_combo.setCurrentIndex(dialog.tmdb_endpoint_combo.findData("__custom__"))
    dialog.tmdb_proxy_base_url_edit.setText(" https://tmdb.example.com/ ")
    dialog.bangumi_access_token_edit.setText(" bgm-demo-token ")
    dialog._save()

    assert config.metadata_enhancement_enabled is False
    assert config.metadata_douban_cookie == "bid=demo; ll=118282"
    assert config.metadata_tmdb_api_key == "tmdb-demo-key"
    assert config.metadata_tmdb_proxy_base_url == "https://tmdb.example.com"
    assert config.metadata_bangumi_access_token == "bgm-demo-token"
    assert len(saved) == 1


def test_advanced_settings_dialog_saves_telegram_api_config(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved: list[AppConfig] = []
    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: saved.append(config))
    qtbot.addWidget(dialog)

    dialog.telegram_api_id_edit.setText("12345")
    dialog.telegram_api_hash_edit.setText(" telegram-api-hash ")
    dialog._save()

    assert config.telegram_api_id == 12345
    assert config.telegram_api_hash == "telegram-api-hash"
    assert len(saved) == 1


def test_advanced_settings_dialog_saves_preset_tmdb_proxy(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved: list[AppConfig] = []
    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: saved.append(config))
    qtbot.addWidget(dialog)

    dialog.tmdb_endpoint_combo.setCurrentIndex(
        dialog.tmdb_endpoint_combo.findData("https://tmdb.8866033.xyz")
    )
    dialog._save()

    assert config.metadata_tmdb_proxy_base_url == "https://tmdb.8866033.xyz"
    assert len(saved) == 1


def test_advanced_settings_dialog_displays_tmdb_speed_results(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: None)
    qtbot.addWidget(dialog)

    dialog._apply_tmdb_speed_results(
        [
            {"label": "官方 API", "value": "", "elapsed_ms": 120, "status": "OK"},
            {
                "label": "Worker 8866033.xyz",
                "value": "https://tmdb.8866033.xyz",
                "elapsed_ms": 80,
                "status": "OK",
            },
        ]
    )

    official_index = dialog.tmdb_endpoint_combo.findData("")
    worker_index = dialog.tmdb_endpoint_combo.findData("https://tmdb.8866033.xyz")
    assert "120 ms" in dialog.tmdb_endpoint_combo.itemText(official_index)
    assert "80 ms" in dialog.tmdb_endpoint_combo.itemText(worker_index)


def test_advanced_settings_dialog_saves_source_enablement(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved: list[AppConfig] = []
    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: saved.append(config))
    qtbot.addWidget(dialog)

    dialog.danmaku_source_checkboxes["youku"].setChecked(False)
    dialog.metadata_source_checkboxes["tmdb"].setChecked(False)
    dialog.metadata_source_checkboxes["official_douban"].setChecked(False)
    dialog._save()

    assert config.disabled_danmaku_provider_ids == ["youku"]
    assert config.disabled_metadata_provider_ids == ["official_douban", "tmdb"]
    assert len(saved) == 1


def test_advanced_settings_dialog_exposes_migu_danmaku_source_only(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved: list[AppConfig] = []
    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: saved.append(config))
    qtbot.addWidget(dialog)

    assert dialog.danmaku_source_checkboxes["migu"].text() == "咪咕"
    assert "migu" not in dialog.metadata_source_checkboxes

    dialog.danmaku_source_checkboxes["migu"].setChecked(False)
    dialog._save()

    assert "migu" in config.disabled_danmaku_provider_ids
    assert "migu" not in config.disabled_metadata_provider_ids


def test_advanced_settings_dialog_exposes_renren_danmaku_source(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved: list[AppConfig] = []
    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: saved.append(config))
    qtbot.addWidget(dialog)

    assert dialog.danmaku_source_checkboxes["renren"].text() == "人人"
    assert "renren" not in dialog.metadata_source_checkboxes

    dialog.danmaku_source_checkboxes["renren"].setChecked(False)
    dialog._save()

    assert "renren" in config.disabled_danmaku_provider_ids
    assert "renren" not in config.disabled_metadata_provider_ids


def test_advanced_settings_dialog_arranges_source_checkboxes_in_columns(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.metadata_source_group.layout().columnCount() == 3
    assert dialog.danmaku_source_group.layout().columnCount() == 3


def test_advanced_settings_dialog_adds_appearance_tab_and_populates_theme_mode(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(AppConfig(theme_mode="dark"), save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.settings_tabs.tabText(0) == "外观"
    assert dialog.settings_tabs.tabText(1) == "播放设置"
    assert dialog.theme_mode_combo.currentData() == "dark"


def test_advanced_settings_dialog_uses_larger_default_size(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.size().width() == 1100
    assert dialog.size().height() == 640


def test_advanced_settings_dialog_saves_theme_mode_and_calls_theme_refresh(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved: list[str] = []
    refreshed: list[bool] = []
    config = AppConfig(theme_mode="system")
    dialog = AdvancedSettingsDialog(
        config,
        save_config=lambda: saved.append(config.theme_mode),
        apply_theme=lambda: refreshed.append(True),
    )
    qtbot.addWidget(dialog)

    dialog.theme_mode_combo.setCurrentIndex(dialog.theme_mode_combo.findData("light"))
    dialog.save_button.click()

    assert saved == ["light"]
    assert refreshed == [True]
    assert config.theme_mode == "light"


def test_advanced_settings_dialog_applies_branded_combobox_styles(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert "QComboBox::drop-down" in dialog.theme_mode_combo.styleSheet()
    assert dialog.theme_mode_combo.styleSheet() == dialog.home_mode_combo.styleSheet()
    assert dialog.theme_mode_combo.styleSheet() == dialog.network_proxy_mode_combo.styleSheet()
    assert dialog.theme_mode_combo.property("flat_combo_border_color") == dialog.home_mode_combo.property(
        "flat_combo_border_color"
    )
    assert dialog.theme_mode_combo.property("flat_combo_border_color") == dialog.network_proxy_mode_combo.property(
        "flat_combo_border_color"
    )


def test_advanced_settings_dialog_keeps_home_mode_in_separate_appearance_area(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.appearance_group.title() == "外观"
    assert dialog.homepage_group.title() == "首页模式"
    assert dialog.appearance_group.layout().labelForField(dialog.theme_mode_combo).text() == "界面主题"
    assert dialog.appearance_group.layout().labelForField(dialog.home_mode_combo) is None
    assert dialog.homepage_group.layout().labelForField(dialog.home_mode_combo).text() == "模式"


def test_advanced_settings_dialog_applies_branded_line_edit_styles(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(AppConfig(network_proxy_mode="system"), save_config=lambda: None)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(dialog.isVisible)

    assert "QLineEdit:disabled" in dialog.network_proxy_url_edit.styleSheet()
    assert dialog.tmdb_api_key_edit.styleSheet() == dialog.mpv_cache_size_edit.styleSheet()
    assert dialog.tmdb_api_key_edit.height() == 42
    assert dialog.tmdb_proxy_base_url_edit.height() == 42
    assert dialog.network_proxy_url_edit.height() == 42


def test_advanced_settings_dialog_tab_bar_uses_pointing_hand_cursor(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.settings_tabs.tabBar().cursor().shape() == Qt.CursorShape.PointingHandCursor


def test_advanced_settings_dialog_hides_telegram_login_controls_when_logged_in(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    user = SimpleNamespace(display_name="Alice", username="alice")
    chat = SimpleNamespace(id=-1001, title="电影频道", kind="channel", enabled=True, last_indexed_msg_id=42)

    class TelegramController:
        def get_local_user_info(self):
            return user

        def list_local_chats(self):
            return [chat]

    dialog = AdvancedSettingsDialog(
        AppConfig(telegram_api_id=12345, telegram_api_hash="hash"),
        save_config=lambda: None,
        telegram_controller=TelegramController(),
    )
    qtbot.addWidget(dialog)

    qtbot.waitUntil(lambda: dialog.telegram_status_label.text() == "已登录：Alice @alice", timeout=1000)
    assert dialog.telegram_status_label.text() == "已登录：Alice @alice"
    assert dialog.telegram_qr_login_button.isHidden() is True
    assert dialog.telegram_phone_login_button.isHidden() is True
    assert dialog.telegram_logout_button.isHidden() is False
    assert dialog.telegram_refresh_chats_button.isEnabled() is True
    assert dialog.telegram_channel_table.rowCount() == 1
    assert [
        dialog.telegram_channel_table.horizontalHeaderItem(index).text()
        for index in range(dialog.telegram_channel_table.columnCount())
    ] == ["搜索", "浏览", "名称", "类型", "可见性", "Web访问", "ID", "已索引消息"]
    assert dialog.telegram_channel_table.item(0, 4).text() == "私密"
    assert dialog.telegram_channel_table.item(0, 5).text() == "Web不可访问"


def test_advanced_settings_dialog_logout_clears_telegram_channels(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    chat = SimpleNamespace(id=-1001, title="电影频道", kind="channel", enabled=True, last_indexed_msg_id=42)

    class TelegramController:
        def __init__(self) -> None:
            self.user = SimpleNamespace(display_name="Alice", username="alice")

        def get_local_user_info(self):
            return self.user

        def list_local_chats(self):
            return [chat]

        def logout_local(self):
            self.user = None

    controller = TelegramController()
    dialog = AdvancedSettingsDialog(
        AppConfig(telegram_api_id=12345, telegram_api_hash="hash"),
        save_config=lambda: None,
        telegram_controller=controller,
    )
    qtbot.addWidget(dialog)

    qtbot.waitUntil(lambda: dialog.telegram_channel_table.rowCount() == 1, timeout=1000)
    assert dialog.telegram_channel_table.rowCount() == 1

    dialog._logout_telegram()

    assert controller.user is None
    assert dialog.telegram_status_label.text() == "未登录"
    assert dialog.telegram_channel_table.rowCount() == 0
    assert dialog.telegram_qr_login_button.isHidden() is False
    assert dialog.telegram_logout_button.isHidden() is True
    assert dialog.telegram_channel_status_label.text() == "已退出登录"


def test_advanced_settings_dialog_refreshes_telegram_chats_in_background(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    started = threading.Event()
    release = threading.Event()
    refreshed_chat = SimpleNamespace(id=-1001, title="电影频道", kind="channel", enabled=True, last_indexed_msg_id=42)

    class TelegramController:
        def __init__(self) -> None:
            self.chats = []

        def get_local_user_info(self):
            return SimpleNamespace(display_name="Alice", username="alice")

        def list_local_chats(self):
            return list(self.chats)

        def refresh_local_chats(self):
            started.set()
            release.wait(1)
            self.chats = [refreshed_chat]
            return list(self.chats)

    dialog = AdvancedSettingsDialog(
        AppConfig(telegram_api_id=12345, telegram_api_hash="hash"),
        save_config=lambda: None,
        telegram_controller=TelegramController(),
    )
    qtbot.addWidget(dialog)

    dialog._refresh_telegram_chats()

    qtbot.waitUntil(started.is_set, timeout=1000)
    assert dialog.telegram_refresh_chats_button.isEnabled() is False
    assert dialog.telegram_status_label.text() == "已登录：Alice @alice"
    assert dialog.telegram_channel_status_label.text() == "频道刷新中..."

    release.set()
    qtbot.waitUntil(lambda: dialog.telegram_channel_status_label.text() == "已刷新 1 个频道/群组", timeout=1000)
    assert dialog.telegram_channel_table.rowCount() == 1
    assert dialog.telegram_status_label.text() == "已登录：Alice @alice"
    assert dialog.telegram_channel_status_label.text() == "已刷新 1 个频道/群组"


def test_advanced_settings_dialog_filters_and_sorts_telegram_channels(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    chats = [
        SimpleNamespace(
            id=-2,
            title="电影频道",
            kind="channel",
            username="",
            enabled=True,
            browse_enabled=False,
            last_indexed_msg_id=2,
        ),
        SimpleNamespace(
            id=-100,
            title="资源群",
            kind="group",
            username="share",
            enabled=True,
            browse_enabled=True,
            last_indexed_msg_id=100,
        ),
        SimpleNamespace(
            id=-10,
            title="未配置频道",
            kind="channel",
            username="",
            enabled=False,
            browse_enabled=False,
            last_indexed_msg_id=10,
        ),
    ]

    class TelegramController:
        def get_local_user_info(self):
            return SimpleNamespace(display_name="Alice", username="alice")

        def list_local_chats(self):
            return chats

    dialog = AdvancedSettingsDialog(
        AppConfig(telegram_api_id=12345, telegram_api_hash="hash"),
        save_config=lambda: None,
        telegram_controller=TelegramController(),
    )
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.telegram_channel_table.rowCount() == 3, timeout=1000)

    def row_for_title(title: str) -> int:
        for row in range(dialog.telegram_channel_table.rowCount()):
            if dialog.telegram_channel_table.item(row, 2).text() == title:
                return row
        raise AssertionError(f"missing row: {title}")

    def visible_titles() -> list[str]:
        return [
            dialog.telegram_channel_table.item(row, 2).text()
            for row in range(dialog.telegram_channel_table.rowCount())
            if not dialog.telegram_channel_table.isRowHidden(row)
        ]

    private_row = row_for_title("电影频道")
    public_row = row_for_title("资源群")
    assert dialog.telegram_channel_usage_filter_combo.findData("listen") == -1
    assert dialog.telegram_channel_table.columnCount() == 8
    assert dialog.telegram_channel_table.horizontalHeaderItem(2).text() == "名称"
    assert dialog.telegram_channel_table.item(private_row, 4).text() == "私密"
    assert dialog.telegram_channel_table.item(private_row, 5).text() == "Web不可访问"
    assert dialog.telegram_channel_table.cellWidget(private_row, 0).isEnabled() is True
    assert dialog.telegram_channel_table.item(public_row, 4).text() == "公开"
    assert dialog.telegram_channel_table.item(public_row, 5).text() == "可访问"
    assert dialog.telegram_channel_table.item(public_row, 0).text() == ""
    assert dialog.telegram_channel_table.cellWidget(public_row, 0).isEnabled() is False

    dialog.telegram_channel_search_edit.setText("资源")
    assert visible_titles() == ["资源群"]
    assert dialog.telegram_channel_count_label.text() == "1/3"

    dialog.telegram_channel_search_edit.clear()
    dialog.telegram_channel_usage_filter_combo.setCurrentIndex(
        dialog.telegram_channel_usage_filter_combo.findData("")
    )
    dialog.telegram_channel_type_filter_combo.setCurrentIndex(
        dialog.telegram_channel_type_filter_combo.findData("group")
    )
    assert visible_titles() == ["资源群"]

    dialog.telegram_channel_type_filter_combo.setCurrentIndex(
        dialog.telegram_channel_type_filter_combo.findData("")
    )
    dialog.telegram_channel_visibility_filter_combo.setCurrentIndex(
        dialog.telegram_channel_visibility_filter_combo.findData("public")
    )
    assert visible_titles() == ["资源群"]

    dialog.telegram_channel_visibility_filter_combo.setCurrentIndex(
        dialog.telegram_channel_visibility_filter_combo.findData("private")
    )
    assert visible_titles() == ["电影频道", "未配置频道"]

    dialog.telegram_channel_visibility_filter_combo.setCurrentIndex(
        dialog.telegram_channel_visibility_filter_combo.findData("")
    )
    dialog.telegram_channel_table.sortItems(6, Qt.SortOrder.AscendingOrder)

    assert [
        int(dialog.telegram_channel_table.item(row, 6).text())
        for row in range(dialog.telegram_channel_table.rowCount())
    ] == [-100, -10, -2]


def test_advanced_settings_dialog_checks_telegram_status_in_background(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    release = threading.Event()

    class TelegramController:
        def get_local_user_info(self):
            release.wait(1)
            return SimpleNamespace(display_name="Alice", username="alice")

        def list_local_chats(self):
            return []

    started_at = time.monotonic()
    dialog = AdvancedSettingsDialog(
        AppConfig(telegram_api_id=12345, telegram_api_hash="hash"),
        save_config=lambda: None,
        telegram_controller=TelegramController(),
    )
    elapsed = time.monotonic() - started_at
    qtbot.addWidget(dialog)

    assert elapsed < 0.2
    assert dialog.telegram_status_label.text() == "检查登录状态中..."

    release.set()
    qtbot.waitUntil(lambda: dialog.telegram_status_label.text() == "已登录：Alice @alice", timeout=1000)


def test_advanced_settings_dialog_starts_qr_login_in_background(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    started = threading.Event()
    release = threading.Event()
    saved: list[bool] = []

    class TelegramController:
        def get_local_user_info(self):
            return None

        def list_local_chats(self):
            return []

        def start_local_qr_login(self):
            started.set()
            release.wait(1)
            return SimpleNamespace(url="tg://login?token=demo")

    dialog = AdvancedSettingsDialog(
        AppConfig(telegram_api_id=12345, telegram_api_hash="hash"),
        save_config=lambda: saved.append(True),
        telegram_controller=TelegramController(),
    )
    qtbot.addWidget(dialog)

    started_at = time.monotonic()
    dialog._start_telegram_qr_login()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.2
    qtbot.waitUntil(started.is_set, timeout=1000)
    assert dialog.telegram_qr_login_button.isEnabled() is False
    assert dialog.telegram_status_label.text() == "正在生成扫码二维码..."

    release.set()
    qtbot.waitUntil(lambda: dialog.telegram_status_label.text() == "等待扫码确认", timeout=1000)
    assert saved == [True]
    assert dialog.telegram_qr_login_button.isEnabled() is True
    assert dialog.telegram_qr_label.pixmap() is not None


def test_advanced_settings_dialog_loads_network_proxy_values(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig(
        network_proxy_mode="socks5",
        network_proxy_url="socks5://user:pass@127.0.0.1:1080",
        network_proxy_bypass_rules=["localhost", "127.0.0.1"],
    )
    dialog = AdvancedSettingsDialog(config, save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.settings_tabs.tabText(0) == "外观"
    assert dialog.settings_tabs.tabText(1) == "播放设置"
    assert dialog.settings_tabs.tabText(2) == "YouTube"
    assert dialog.settings_tabs.tabText(3) == "元数据"
    assert dialog.settings_tabs.tabText(4) == "AI"
    assert dialog.settings_tabs.tabText(5) == "网络代理"
    assert dialog.network_proxy_mode_combo.currentData() == "socks5"
    assert dialog.network_proxy_url_edit.text() == "socks5://user:pass@127.0.0.1:1080"
    assert dialog.network_proxy_bypass_rules_edit.toPlainText() == "localhost\n127.0.0.1"


def test_advanced_settings_dialog_disables_proxy_url_for_system_mode(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(AppConfig(network_proxy_mode="system"), save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.network_proxy_url_edit.isEnabled() is False


def test_advanced_settings_dialog_toggles_proxy_url_enabled_state(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(AppConfig(network_proxy_mode="direct"), save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.network_proxy_url_edit.isEnabled() is False

    dialog.network_proxy_mode_combo.setCurrentIndex(dialog.network_proxy_mode_combo.findData("http"))

    assert dialog.network_proxy_url_edit.isEnabled() is True

    dialog.network_proxy_mode_combo.setCurrentIndex(dialog.network_proxy_mode_combo.findData("system"))

    assert dialog.network_proxy_url_edit.isEnabled() is False


def test_advanced_settings_dialog_saves_trimmed_network_proxy_values(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved: list[AppConfig] = []
    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: saved.append(config))
    qtbot.addWidget(dialog)

    dialog.network_proxy_mode_combo.setCurrentIndex(dialog.network_proxy_mode_combo.findData("socks5"))
    dialog.network_proxy_url_edit.setText(" socks5://user:pass@127.0.0.1:1080 ")
    dialog.network_proxy_bypass_rules_edit.setPlainText(" localhost \n127.0.0.1\n\n")
    dialog._save()

    assert config.network_proxy_mode == "socks5"
    assert config.network_proxy_url == "socks5://user:pass@127.0.0.1:1080"
    assert config.network_proxy_bypass_rules == ["localhost", "127.0.0.1"]
    assert len(saved) == 1


def test_advanced_settings_dialog_rejects_invalid_proxy_url(qtbot, monkeypatch) -> None:
    from atv_player.ui import advanced_settings_dialog as module
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    messages: list[str] = []

    def fake_warning(_parent, _title: str, text: str) -> int:
        messages.append(text)
        return 0

    monkeypatch.setattr(module.QMessageBox, "warning", fake_warning)
    saved: list[AppConfig] = []
    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: saved.append(dialog._config))
    qtbot.addWidget(dialog)

    dialog.network_proxy_mode_combo.setCurrentIndex(dialog.network_proxy_mode_combo.findData("socks5"))
    dialog.network_proxy_url_edit.setText("http://127.0.0.1:7890")
    dialog._save()

    assert messages == ["SOCKS5 模式要求 socks5:// 代理地址"]
    assert saved == []


def test_advanced_settings_dialog_adds_playback_tab_and_populates_existing_values(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig(
        youtube_cookie_browser="firefox",
        mpv_render_profile="vulkan",
        mpv_cache_size_mb=1024,
        mpv_hwdec_mode="no",
        mpv_network_timeout_seconds=20,
        mpv_default_readahead_secs=35,
        mpv_extra_options="cache-pause-wait=9\nstream-buffer-size=8M",
        playback_auto_switch_source_on_failure=True,
    )
    dialog = AdvancedSettingsDialog(config, save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.settings_tabs.tabText(0) == "外观"
    assert dialog.settings_tabs.tabText(1) == "播放设置"
    assert dialog.playback_auto_switch_source_on_failure_checkbox.isChecked() is True
    assert dialog.youtube_cookie_browser_combo.currentData() == "firefox"
    assert dialog.mpv_cache_size_edit.text() == "1024"
    assert dialog.mpv_hwdec_mode_combo.currentData() == "vulkan"
    assert dialog.mpv_network_timeout_edit.text() == "20"
    assert dialog.mpv_default_readahead_edit.text() == "35"
    assert dialog.mpv_extra_options_edit.toPlainText() == "cache-pause-wait=9\nstream-buffer-size=8M"


def test_advanced_settings_dialog_adds_youtube_tab_and_populates_preferences(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig(
        youtube_cookie_browser="firefox",
        youtube_max_height=1440,
        youtube_video_codec="av1",
        youtube_default_subtitle_lang="zh-TW",
        youtube_default_audio_lang="en",
        youtube_metadata_language="zh-CN",
        youtube_region="CN",
    )
    dialog = AdvancedSettingsDialog(config, save_config=lambda: None)
    qtbot.addWidget(dialog)

    tab_labels = [dialog.settings_tabs.tabText(index) for index in range(dialog.settings_tabs.count())]
    assert tab_labels[:3] == ["外观", "播放设置", "YouTube"]
    assert dialog.youtube_cookie_browser_combo.currentData() == "firefox"
    assert dialog.youtube_max_height_combo.currentData() == 1440
    assert dialog.youtube_video_codec_combo.currentData() == "av1"
    assert dialog.youtube_default_subtitle_combo.currentData() == "zh-TW"
    assert dialog.youtube_default_audio_combo.currentData() == "en"
    assert dialog.youtube_metadata_language_combo.currentData() == "zh-CN"
    assert dialog.youtube_region_combo.currentData() == "CN"


def test_advanced_settings_dialog_shows_youtube_category_source_controls(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig(
        youtube_category_source_type="remote",
        youtube_category_source_value="http://example.test/youtube.json",
    )
    dialog = AdvancedSettingsDialog(config, save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.youtube_category_source_combo.currentData() == "remote"
    assert dialog.youtube_category_source_edit.text() == "http://example.test/youtube.json"
    assert dialog.youtube_category_source_edit.isEnabled() is True
    assert dialog.youtube_category_local_path_edit.isEnabled() is False


def test_advanced_settings_dialog_saves_youtube_category_source(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved = []
    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: saved.append(config))
    qtbot.addWidget(dialog)

    dialog.youtube_category_source_combo.setCurrentIndex(
        dialog.youtube_category_source_combo.findData("local")
    )
    dialog.youtube_category_local_path_edit.setText("/tmp/youtube.json")
    dialog._save()

    assert saved == [config]
    assert config.youtube_category_source_type == "local"
    assert config.youtube_category_source_value == "/tmp/youtube.json"


def test_advanced_settings_dialog_test_load_reports_counts(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig(
        youtube_category_source_type="remote",
        youtube_category_source_value="http://example.test/youtube.json",
    )
    dialog = AdvancedSettingsDialog(
        config,
        save_config=lambda: None,
        youtube_category_text_loader=lambda _url: '{"class":[{"type_id":"電影","type_name":"電影"}],"filters":{}}',
    )
    qtbot.addWidget(dialog)

    dialog._test_youtube_category_source()

    assert "1 个分类" in dialog.youtube_category_status_label.text()


def test_advanced_settings_dialog_refresh_cache_updates_config(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved = []
    config = AppConfig(
        youtube_category_source_type="remote",
        youtube_category_source_value="http://example.test/youtube.json",
    )
    dialog = AdvancedSettingsDialog(
        config,
        save_config=lambda: saved.append(config.youtube_category_cache_json),
        youtube_category_text_loader=lambda _url: '{"class":[{"type_id":"電影","type_name":"電影"}],"filters":{}}',
    )
    qtbot.addWidget(dialog)

    dialog._refresh_youtube_category_cache()

    assert config.youtube_category_cache_json.startswith('{"class"')
    assert saved == [config.youtube_category_cache_json]


def test_advanced_settings_dialog_saves_youtube_preferences(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved: list[AppConfig] = []
    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: saved.append(config))
    qtbot.addWidget(dialog)

    dialog.youtube_cookie_browser_combo.setCurrentIndex(dialog.youtube_cookie_browser_combo.findData("chrome"))
    dialog.youtube_max_height_combo.setCurrentIndex(dialog.youtube_max_height_combo.findData(2160))
    dialog.youtube_video_codec_combo.setCurrentIndex(dialog.youtube_video_codec_combo.findData("av1"))
    dialog.youtube_default_subtitle_combo.setCurrentIndex(dialog.youtube_default_subtitle_combo.findData("en"))
    dialog.youtube_default_audio_combo.setCurrentIndex(dialog.youtube_default_audio_combo.findData("zh"))
    dialog.youtube_metadata_language_combo.setCurrentIndex(dialog.youtube_metadata_language_combo.findData("zh-HK"))
    dialog.youtube_region_combo.setCurrentIndex(dialog.youtube_region_combo.findData("JP"))
    dialog._save()

    assert config.youtube_cookie_browser == "chrome"
    assert config.youtube_max_height == 2160
    assert config.youtube_video_codec == "av1"
    assert config.youtube_default_subtitle_lang == "en"
    assert config.youtube_default_audio_lang == "zh"
    assert config.youtube_metadata_language == "zh-HK"
    assert config.youtube_region == "JP"
    assert len(saved) == 1


def test_advanced_settings_dialog_saves_trimmed_playback_settings(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved: list[AppConfig] = []
    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: saved.append(config))
    qtbot.addWidget(dialog)

    dialog.playback_auto_switch_source_on_failure_checkbox.setChecked(True)
    dialog.youtube_cookie_browser_combo.setCurrentIndex(dialog.youtube_cookie_browser_combo.findData("chrome"))
    dialog.mpv_cache_size_edit.setText(" 768 ")
    dialog.mpv_hwdec_mode_combo.setCurrentIndex(dialog.mpv_hwdec_mode_combo.findData("quality"))
    dialog.mpv_network_timeout_edit.setText(" 22 ")
    dialog.mpv_default_readahead_edit.setText(" 40 ")
    dialog.mpv_extra_options_edit.setPlainText(" cache-pause-wait=8 \nstream-buffer-size=6M ")
    dialog._save()

    assert config.playback_auto_switch_source_on_failure is True
    assert config.youtube_cookie_browser == "chrome"
    assert config.mpv_cache_size_mb == 768
    assert config.mpv_render_profile == "quality"
    assert config.mpv_network_timeout_seconds == 22
    assert config.mpv_default_readahead_secs == 40
    assert config.mpv_extra_options == "cache-pause-wait=8\nstream-buffer-size=6M"
    assert len(saved) == 1


def test_advanced_settings_dialog_saves_vulkan_render_profile(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: None)
    qtbot.addWidget(dialog)

    dialog.mpv_hwdec_mode_combo.setCurrentIndex(dialog.mpv_hwdec_mode_combo.findData("vulkan"))
    dialog._save()

    assert config.mpv_render_profile == "vulkan"


def test_advanced_settings_dialog_loads_m3u_proxy_segment_prefetch_size(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(
        AppConfig(m3u_proxy_segment_prefetch_size=4),
        save_config=lambda: None,
    )
    qtbot.addWidget(dialog)

    assert dialog.m3u_proxy_segment_prefetch_size_edit.text() == "4"
    assert dialog.m3u_proxy_segment_prefetch_size_edit.placeholderText() == "0 - 10"


def test_advanced_settings_dialog_saves_m3u_proxy_segment_prefetch_size(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved: list[AppConfig] = []
    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: saved.append(config))
    qtbot.addWidget(dialog)

    dialog.m3u_proxy_segment_prefetch_size_edit.setText(" 0 ")
    dialog._save()

    assert config.m3u_proxy_segment_prefetch_size == 0
    assert len(saved) == 1


def test_advanced_settings_dialog_rejects_invalid_m3u_proxy_segment_prefetch_size(qtbot, monkeypatch) -> None:
    from atv_player.ui import advanced_settings_dialog as module
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    messages: list[str] = []

    def fake_warning(_parent, _title: str, text: str) -> int:
        messages.append(text)
        return 0

    monkeypatch.setattr(module.QMessageBox, "warning", fake_warning)
    saved: list[AppConfig] = []
    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: saved.append(dialog._config))
    qtbot.addWidget(dialog)

    dialog.m3u_proxy_segment_prefetch_size_edit.setText("11")
    dialog._save()

    assert messages == ["m3u代理分片预取大小必须在 0 到 10 之间"]
    assert saved == []


def test_advanced_settings_dialog_rejects_invalid_extra_mpv_options(qtbot, monkeypatch) -> None:
    from atv_player.ui import advanced_settings_dialog as module
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    messages: list[str] = []

    def fake_warning(_parent, _title: str, text: str) -> int:
        messages.append(text)
        return 0

    monkeypatch.setattr(module.QMessageBox, "warning", fake_warning)
    saved: list[AppConfig] = []
    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: saved.append(dialog._config))
    qtbot.addWidget(dialog)

    dialog.mpv_extra_options_edit.setPlainText("cache-pause-wait\n=broken")
    dialog._save()

    assert messages == ["更多 MPV 配置第 1 行必须是 key=value 格式"]
    assert saved == []


def test_advanced_settings_dialog_loads_episode_title_enhancement_checkbox(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig(
        metadata_enhancement_enabled=True,
        metadata_tmdb_api_key="tmdb-demo-key",
        episode_title_enhancement_enabled=True,
    )
    dialog = AdvancedSettingsDialog(config, save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.episode_title_enhancement_checkbox.isChecked() is True
    assert dialog.episode_title_enhancement_checkbox.isEnabled() is True


def test_advanced_settings_dialog_saves_episode_title_enhancement_checkbox(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    saved: list[AppConfig] = []
    config = AppConfig(metadata_enhancement_enabled=True)
    dialog = AdvancedSettingsDialog(config, save_config=lambda: saved.append(config))
    qtbot.addWidget(dialog)

    dialog.episode_title_enhancement_checkbox.setChecked(True)
    dialog.save_button.click()

    assert saved[-1].episode_title_enhancement_enabled is True


def test_advanced_settings_dialog_adds_logs_tab_with_logging_toggle(qtbot) -> None:
    from atv_player.log_store import AppLogFilter
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    class FakeService:
        def load_records(self, *, limit: int, log_filter: AppLogFilter):
            del limit, log_filter
            return []

    dialog = AdvancedSettingsDialog(
        AppConfig(logging_enabled=False),
        save_config=lambda: None,
        app_log_service=FakeService(),
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(dialog.isVisible)

    tab_labels = [dialog.settings_tabs.tabText(index) for index in range(dialog.settings_tabs.count())]
    assert "日志" in tab_labels
    assert dialog.logging_enabled_checkbox.isChecked() is False
    assert "仅可查看历史日志" in dialog.log_console.status_label.text()


def test_advanced_settings_dialog_defers_slow_initial_content_until_visible(
    qtbot,
    monkeypatch,
) -> None:
    from atv_player import cache_management
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    calls: list[str] = []

    def fake_build_cache_summary():
        calls.append("cache")
        return cache_management.CacheSummary(
            root=cache_management.app_cache_dir(),
            categories=(),
            total_size_bytes=0,
            total_file_count=0,
        )

    class FakeService:
        def load_records(self, *, limit: int, log_filter):
            del limit, log_filter
            calls.append("logs")
            return []

    monkeypatch.setattr(cache_management, "build_cache_summary", fake_build_cache_summary)

    dialog = AdvancedSettingsDialog(
        AppConfig(),
        save_config=lambda: None,
        app_log_service=FakeService(),
    )
    qtbot.addWidget(dialog)

    assert calls == []

    dialog.show()
    qtbot.waitUntil(lambda: calls == ["cache", "logs"])


def test_advanced_settings_dialog_loads_initial_content_without_blocking_paint(
    qtbot,
    monkeypatch,
) -> None:
    from atv_player import cache_management
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    cache_started = threading.Event()
    release_cache = threading.Event()

    def slow_build_cache_summary():
        cache_started.set()
        assert release_cache.wait(timeout=5)
        return cache_management.CacheSummary(
            root=cache_management.app_cache_dir(),
            categories=(),
            total_size_bytes=0,
            total_file_count=0,
        )

    class FakeService:
        def load_records(self, *, limit: int, log_filter):
            del limit, log_filter
            return []

    monkeypatch.setattr(cache_management, "build_cache_summary", slow_build_cache_summary)

    dialog = AdvancedSettingsDialog(
        AppConfig(),
        save_config=lambda: None,
        app_log_service=FakeService(),
    )
    qtbot.addWidget(dialog)

    dialog.show()
    qtbot.waitUntil(cache_started.is_set)

    assert dialog.isVisible() is True
    assert dialog.cache_root_label.text() == "缓存统计加载中..."

    release_cache.set()
    qtbot.waitUntil(lambda: dialog.cache_root_label.text().startswith("缓存目录："))


def test_advanced_settings_dialog_adds_cache_management_tab_and_summary(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    from atv_player import cache_management
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    monkeypatch.setattr(cache_management, "app_cache_dir", lambda: tmp_path)
    (tmp_path / "posters").mkdir(parents=True)
    (tmp_path / "posters" / "poster.img").write_bytes(b"123")
    (tmp_path / "metadata" / "detail").mkdir(parents=True)
    (tmp_path / "metadata" / "detail" / "item.json").write_bytes(b"12")

    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: None)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(
        lambda: dialog.cache_category_table.rowCount() == len(cache_management.CACHE_CATEGORIES)
    )

    tab_labels = [
        dialog.settings_tabs.tabText(index)
        for index in range(dialog.settings_tabs.count())
    ]
    assert "缓存管理" in tab_labels
    assert dialog.cache_root_label.text().endswith(str(tmp_path))
    assert dialog.cache_total_size_label.text() == "总大小：5 B"
    assert dialog.cache_total_files_label.text() == "文件数量：2"
    assert dialog.cache_category_table.rowCount() == len(cache_management.CACHE_CATEGORIES)

    labels = [
        dialog.cache_category_table.item(row, 0).text()
        for row in range(dialog.cache_category_table.rowCount())
    ]
    assert labels == [
        "插件缓存",
        "海报缓存",
        "元数据缓存",
        "弹幕缓存",
        "播放缓存",
        "其他缓存",
    ]


def test_advanced_settings_dialog_clears_single_cache_category(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    from atv_player import cache_management
    from atv_player.ui import advanced_settings_dialog as module
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    monkeypatch.setattr(cache_management, "app_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(
        module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: module.QMessageBox.StandardButton.Yes,
    )
    (tmp_path / "posters").mkdir(parents=True)
    (tmp_path / "posters" / "poster.img").write_bytes(b"123")
    (tmp_path / "plugins").mkdir(parents=True)
    (tmp_path / "plugins" / "plugin.py").write_bytes(b"12")

    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: None)
    qtbot.addWidget(dialog)

    dialog._clear_cache_category("posters")

    assert list((tmp_path / "posters").iterdir()) == []
    assert (tmp_path / "plugins" / "plugin.py").exists()
    assert dialog.cache_total_size_label.text() == "总大小：2 B"
    assert dialog.cache_total_files_label.text() == "文件数量：1"


def test_advanced_settings_dialog_clears_all_cache(qtbot, tmp_path, monkeypatch) -> None:
    from atv_player import cache_management
    from atv_player.ui import advanced_settings_dialog as module
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    monkeypatch.setattr(cache_management, "app_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(
        module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: module.QMessageBox.StandardButton.Yes,
    )
    (tmp_path / "posters").mkdir(parents=True)
    (tmp_path / "posters" / "poster.img").write_bytes(b"123")
    (tmp_path / "loose.tmp").write_bytes(b"12")

    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: None)
    qtbot.addWidget(dialog)

    dialog.cache_clear_all_button.click()

    assert list(tmp_path.iterdir()) == []
    assert dialog.cache_total_size_label.text() == "总大小：0 B"
    assert dialog.cache_total_files_label.text() == "文件数量：0"


def test_advanced_settings_dialog_clears_old_cache(qtbot, tmp_path, monkeypatch) -> None:
    import os
    import time

    from atv_player import cache_management
    from atv_player.ui import advanced_settings_dialog as module
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    monkeypatch.setattr(cache_management, "app_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(
        module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: module.QMessageBox.StandardButton.Yes,
    )
    messages: list[str] = []
    monkeypatch.setattr(
        module.QMessageBox,
        "information",
        lambda _parent, _title, text: messages.append(text),
    )
    old_file = tmp_path / "posters" / "old.img"
    new_file = tmp_path / "posters" / "new.img"
    old_file.parent.mkdir(parents=True)
    old_file.write_bytes(b"123")
    new_file.write_bytes(b"12")
    now = time.time()
    old_mtime = now - (8 * 24 * 60 * 60)
    new_mtime = now - (2 * 24 * 60 * 60)
    os.utime(old_file, (old_mtime, old_mtime))
    os.utime(new_file, (new_mtime, new_mtime))

    dialog = AdvancedSettingsDialog(AppConfig(), save_config=lambda: None)
    qtbot.addWidget(dialog)
    dialog.cache_old_days_spinbox.setValue(7)

    dialog.cache_clear_old_button.click()

    assert old_file.exists() is False
    assert new_file.exists() is True
    assert dialog.cache_total_size_label.text() == "总大小：2 B"
    assert dialog.cache_total_files_label.text() == "文件数量：1"
    assert messages == ["已删除 1 个旧缓存文件，释放 3 B。"]


def test_advanced_settings_dialog_saves_logging_enabled_toggle(qtbot) -> None:
    from atv_player.log_store import AppLogFilter
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    class FakeService:
        def load_records(self, *, limit: int, log_filter: AppLogFilter):
            del limit, log_filter
            return []

    saved: list[bool] = []
    config = AppConfig(logging_enabled=True)
    dialog = AdvancedSettingsDialog(
        config,
        save_config=lambda: saved.append(config.logging_enabled),
        app_log_service=FakeService(),
    )
    qtbot.addWidget(dialog)

    dialog.logging_enabled_checkbox.setChecked(False)
    dialog.save_button.click()

    assert config.logging_enabled is False
    assert saved == [False]


def test_log_console_widget_filters_and_shows_details(qtbot) -> None:
    from atv_player.log_store import AppLogEvent
    from atv_player.ui.log_console import LogConsoleWidget

    class FakeService:
        def __init__(self) -> None:
            self.loaded_filters: list[tuple[int, str, str, str, str]] = []

        def load_records(self, *, limit: int, log_filter):
            self.loaded_filters.append((limit, log_filter.query, log_filter.source, log_filter.level, log_filter.category))
            return [
                AppLogEvent(
                    timestamp="2026-05-19T12:00:00.000",
                    level="ERROR",
                    source="player",
                    category="playback",
                    message="播放失败: boom",
                    module="atv_player.ui.player_window",
                    vod_id="vod-1",
                    vod_name="测试剧",
                    episode_title="第1集",
                    session_id="session-1",
                    url_summary="https://media.example/1.m3u8",
                )
            ]

        def export_records(self, records, target_path):
            del records, target_path

        def clear(self) -> None:
            return None

    service = FakeService()
    widget = LogConsoleWidget(config=AppConfig(logging_enabled=True), save_config=lambda: None, app_log_service=service)
    qtbot.addWidget(widget)

    widget.search_edit.setText("测试剧")
    widget.source_combo.setCurrentIndex(widget.source_combo.findData("player"))
    widget.level_combo.setCurrentIndex(widget.level_combo.findData("ERROR"))
    widget.category_combo.setCurrentIndex(widget.category_combo.findData("playback"))
    widget.refresh_button.click()

    assert service.loaded_filters[-1] == (2000, "测试剧", "player", "ERROR", "playback")
    assert widget.log_table.rowCount() == 1

    widget.log_table.selectRow(0)
    qtbot.waitUntil(lambda: "播放失败: boom" in widget.detail_view.toPlainText())

    detail_text = widget.detail_view.toPlainText()
    assert "测试剧" in detail_text
    assert "第1集" in detail_text
    assert "https://media.example/1.m3u8" in detail_text


def test_log_console_widget_exports_and_clears_filtered_records(qtbot, monkeypatch, tmp_path) -> None:
    from atv_player.log_store import AppLogEvent
    from atv_player.ui import log_console as module
    from atv_player.ui.log_console import LogConsoleWidget

    class FakeService:
        def __init__(self) -> None:
            self.records = [
                AppLogEvent(
                    timestamp="2026-05-19T12:00:00.000",
                    level="ERROR",
                    source="player",
                    category="playback",
                    message="播放失败: boom",
                    module="atv_player.ui.player_window",
                )
            ]
            self.exported: list[tuple[list[object], str]] = []
            self.clear_calls = 0

        def load_records(self, *, limit: int, log_filter):
            del limit, log_filter
            return list(self.records)

        def export_records(self, records, target_path) -> None:
            self.exported.append((list(records), str(target_path)))
            target_path.write_text(records[0].message, encoding="utf-8")

        def clear(self) -> None:
            self.clear_calls += 1
            self.records = []

    export_path = tmp_path / "logs-export.log"
    monkeypatch.setattr(
        module.QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(export_path), "Log Files (*.log)"),
    )
    monkeypatch.setattr(
        module.QMessageBox,
        "question",
        lambda *args, **kwargs: module.QMessageBox.StandardButton.Yes,
    )
    service = FakeService()
    widget = LogConsoleWidget(config=AppConfig(), save_config=lambda: None, app_log_service=service)
    qtbot.addWidget(widget)

    widget.export_button.click()

    assert service.exported == [([service.records[0]], str(export_path))]
    assert export_path.read_text(encoding="utf-8") == "播放失败: boom"

    widget.clear_button.click()

    assert service.clear_calls == 1
    assert widget.log_table.rowCount() == 0


def test_history_page_search_styles_follow_resolved_dark_theme(qtbot) -> None:
    from PySide6.QtWidgets import QApplication

    from atv_player.ui.history_page import HistoryPage
    from atv_player.ui.theme import ThemeManager, install_theme

    class FakeHistoryController:
        def load_page(self, **kwargs):
            del kwargs
            return [], 0

    app = QApplication.instance() or QApplication([])
    manager = ThemeManager(system_theme_getter=lambda: "dark")
    install_theme(app, manager, "dark")

    page = HistoryPage(controller=FakeHistoryController())
    qtbot.addWidget(page)

    tokens = manager.tokens_for("dark")
    assert tokens.input_border in page.search_edit.styleSheet()
    assert tokens.accent in page.continue_button.styleSheet()
    assert tokens.button_disabled_bg in page.continue_button.styleSheet()
    assert tokens.button_disabled_border in page.continue_button.styleSheet()
    assert tokens.button_disabled_text in page.continue_button.styleSheet()


def test_main_window_open_player_creates_session_without_blocking_ui(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    class SlowPlayerController(FakePlayerController):
        def create_session(
            self,
            vod,
            playlist,
            clicked_index: int,
            playlists=None,
            playlist_index: int = 0,
            source_groups=None,
            source_group_index: int = 0,
            source_index: int = 0,
            source_kind: str = "",
            source_key: str = "",
            detail_resolver=None,
            resolved_vod_by_id=None,
            use_local_history=True,
            restore_history=False,
            playback_loader=None,
            async_playback_loader=False,
            detail_action_runner=None,
            detail_field_runner=None,
            metadata_hydrator=None,
            metadata_scrape_service=None,
            metadata_binding_repository=None,
            episode_title_enhancer=None,
            danmaku_controller=None,
            playback_progress_reporter=None,
            playback_stopper=None,
            playback_history_loader=None,
            playback_history_saver=None,
            initial_log_message="",
            is_placeholder=False,
        ):
            time.sleep(0.15)
            return super().create_session(
                vod,
                playlist,
                clicked_index,
                playlists=playlists,
                playlist_index=playlist_index,
                source_groups=source_groups,
                source_group_index=source_group_index,
                source_index=source_index,
                detail_resolver=detail_resolver,
                resolved_vod_by_id=resolved_vod_by_id,
                use_local_history=use_local_history,
                restore_history=restore_history,
                playback_loader=playback_loader,
                async_playback_loader=async_playback_loader,
                detail_action_runner=detail_action_runner,
                detail_field_runner=detail_field_runner,
                metadata_hydrator=metadata_hydrator,
                metadata_scrape_service=metadata_scrape_service,
                metadata_binding_repository=metadata_binding_repository,
                episode_title_enhancer=episode_title_enhancer,
                danmaku_controller=danmaku_controller,
                playback_progress_reporter=playback_progress_reporter,
                playback_stopper=playback_stopper,
                playback_history_loader=playback_history_loader,
                playback_history_saver=playback_history_saver,
                initial_log_message=initial_log_message,
                is_placeholder=is_placeholder,
            )

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.opened: list[tuple[object, bool]] = []
            self.show_calls = 0
            self.raise_calls = 0
            self.activate_calls = 0
            self.closed_to_main = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            self.show_calls += 1

        def raise_(self) -> None:
            self.raise_calls += 1

        def activateWindow(self) -> None:
            self.activate_calls += 1

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=SlowPlayerController(),
        config=config,
        save_config=lambda: None,
    )
    errors: list[str] = []
    monkeypatch.setattr(window, "show_error", lambda message: errors.append(message))
    qtbot.addWidget(window)
    window.show()
    request = OpenPlayerRequest(
        vod=VodItem(vod_id="vod-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
        clicked_index=0,
        source_mode="detail",
        source_vod_id="vod-1",
    )

    started_at = time.perf_counter()
    window.open_player(request)
    elapsed_seconds = time.perf_counter() - started_at

    assert elapsed_seconds < 0.1
    assert window.isHidden() is False
    assert window.player_window is None

    deadline = time.perf_counter() + 5.0
    while (
        window.player_window is None
        or len(window.player_window.opened) != 1
        or window.isHidden() is False
    ) and time.perf_counter() < deadline:
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    assert errors == []
    assert window.player_window is not None
    assert len(window.player_window.opened) == 1
    assert window.isHidden() is True
    assert config.last_active_window == "player"
    assert config.last_playback_mode == "detail"
    assert config.last_playback_vod_id == "vod-1"
    assert config.last_player_paused is False
    window.close()


def test_main_window_does_not_report_heat_when_player_opens(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            self.closed_to_main = FakeSignal()
            self.global_search_requested = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            return None

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    heat = FakeHeatController()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        save_config=lambda: None,
        heat_controller=heat,
    )
    qtbot.addWidget(window)
    request = OpenPlayerRequest(
        vod=VodItem(
            vod_id="vod-1",
            vod_name="黑袍纠察队",
            detail_fields=[PlaybackDetailField(label="TMDB ID", value="76479")],
        ),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8", vod_id="episode-1")],
        clicked_index=0,
        source_kind="browse",
        source_vod_id="vod-1",
    )

    window._open_player_immediately(request)

    assert heat.media_events == []


def test_main_window_skips_heat_play_start_without_external_media_id(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            self.closed_to_main = FakeSignal()
            self.global_search_requested = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            return None

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    heat = FakeHeatController()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        save_config=lambda: None,
        heat_controller=heat,
    )
    qtbot.addWidget(window)

    window._open_player_immediately(
        OpenPlayerRequest(
            vod=VodItem(vod_id="vod-1", vod_name="未刮削影片"),
            playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
            clicked_index=0,
            source_kind="browse",
        )
    )

    assert heat.media_events == []


def test_main_window_passes_default_video_cover_loader_to_player_window(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    captured: dict[str, object] = {}

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            captured["loader"] = kwargs.get("default_video_cover_loader")
            self.opened: list[tuple[object, bool]] = []
            self.closed_to_main = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)

    def load_video_cover() -> str:
        return "https://img.example/fallback.jpg"

    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        default_video_cover_loader=load_video_cover,
    )
    qtbot.addWidget(window)

    request = OpenPlayerRequest(
        vod=VodItem(vod_id="vod-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
        clicked_index=0,
    )
    window.open_player(request)

    qtbot.waitUntil(lambda: "loader" in captured)
    assert captured["loader"] is load_video_cover


def test_main_window_passes_heat_controller_to_player_window(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    captured: dict[str, object] = {}

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            captured["heat_controller"] = kwargs.get("heat_controller")
            self.opened: list[tuple[object, bool]] = []
            self.closed_to_main = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    heat = FakeHeatController()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        heat_controller=heat,
    )
    qtbot.addWidget(window)

    window.open_player(
        OpenPlayerRequest(
            vod=VodItem(vod_id="vod-1", vod_name="Movie"),
            playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
            clicked_index=0,
        )
    )

    qtbot.waitUntil(lambda: "heat_controller" in captured)
    assert captured["heat_controller"] is heat


def test_main_window_passes_detail_action_runner_to_player_controller(qtbot) -> None:
    def detail_action_runner(item, action_id):
        return [item, action_id]
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    request = OpenPlayerRequest(
        vod=VodItem(vod_id="vod-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
        clicked_index=0,
        detail_action_runner=detail_action_runner,
    )

    session = window._create_player_session(request)

    assert session["detail_action_runner"] is detail_action_runner


def test_main_window_detail_field_category_click_loads_plugin_results(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            self.opened: list[tuple[object, bool]] = []
            self.closed_to_main = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class RoutedPluginController(FakeStaticController):
        def __init__(self) -> None:
            self.category_calls: list[tuple[str, int]] = []
            self.search_calls: list[tuple[str, int]] = []
            self.build_request_calls: list[str] = []

        def load_categories(self):
            return [type("Category", (), {"type_id": "movie", "type_name": "电影", "filters": []})()]

        def load_items(self, category_id: str, page: int, filters=None):
            self.category_calls.append((category_id, page))
            return [VodItem(vod_id="cat-1", vod_name="分类结果")], 1

        def search_items(self, keyword: str, page: int):
            self.search_calls.append((keyword, page))
            return [VodItem(vod_id="search-1", vod_name="搜索结果")], 1

        def build_request(self, vod_id: str):
            self.build_request_calls.append(vod_id)
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="详情结果"),
                playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
                clicked_index=0,
                source_kind="plugin",
                source_key="plugin-1",
                source_mode="detail",
                source_vod_id=vod_id,
            )

    controller = RoutedPluginController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[{"id": "plugin-1", "title": "插件1", "controller": controller, "search_enabled": True}],
    )
    qtbot.addWidget(window)
    request = controller.build_request("detail-1")

    window.open_player(request)
    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1)

    session = window.player_window.opened[0][0]
    runner = session["detail_field_runner"]
    runner(session["playlist"][0], PlaybackDetailFieldAction(type="category", value="movie"))

    qtbot.waitUntil(lambda: controller.category_calls == [("movie", 1)])
    plugin_page = window._plugin_pages[0][0]
    qtbot.waitUntil(lambda: bool(plugin_page.items) and plugin_page.items[0].vod_name == "分类结果")
    assert window.nav_tabs.currentWidget() is plugin_page


def test_main_window_detail_field_search_click_loads_plugin_results(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            self.opened: list[tuple[object, bool]] = []
            self.closed_to_main = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class RoutedPluginController(FakeStaticController):
        def __init__(self) -> None:
            self.search_calls: list[tuple[str, int]] = []

        def load_categories(self):
            return []

        def search_items(self, keyword: str, page: int):
            self.search_calls.append((keyword, page))
            return [VodItem(vod_id="search-1", vod_name="搜索结果")], 1

        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="详情结果"),
                playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
                clicked_index=0,
                source_kind="plugin",
                source_key="plugin-1",
                source_mode="detail",
                source_vod_id=vod_id,
            )

    controller = RoutedPluginController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[{"id": "plugin-1", "title": "插件1", "controller": controller, "search_enabled": True}],
    )
    qtbot.addWidget(window)
    request = controller.build_request("detail-1")

    window.open_player(request)
    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1)

    session = window.player_window.opened[0][0]
    runner = session["detail_field_runner"]
    runner(session["playlist"][0], PlaybackDetailFieldAction(type="search", value="演员1"))

    qtbot.waitUntil(lambda: controller.search_calls == [("演员1", 1)])
    plugin_page = window._plugin_pages[0][0]
    qtbot.waitUntil(lambda: bool(plugin_page.items) and plugin_page.items[0].vod_name == "搜索结果")
    assert window.nav_tabs.currentWidget() is plugin_page


def test_main_window_detail_field_detail_click_opens_new_plugin_request(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            self.opened: list[tuple[object, bool]] = []
            self.closed_to_main = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class RoutedPluginController(FakeStaticController):
        def __init__(self) -> None:
            self.build_request_calls: list[str] = []

        def load_categories(self):
            return []

        def build_request(self, vod_id: str):
            self.build_request_calls.append(vod_id)
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name=f"详情:{vod_id}"),
                playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
                clicked_index=0,
                source_kind="plugin",
                source_key="plugin-1",
                source_mode="detail",
                source_vod_id=vod_id,
            )

    controller = RoutedPluginController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[{"id": "plugin-1", "title": "插件1", "controller": controller, "search_enabled": True}],
    )
    qtbot.addWidget(window)
    request = controller.build_request("detail-1")

    window.open_player(request)
    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1)

    session = window.player_window.opened[0][0]
    runner = session["detail_field_runner"]
    runner(session["playlist"][0], PlaybackDetailFieldAction(type="detail", value="detail-2"))

    qtbot.waitUntil(lambda: controller.build_request_calls == ["detail-1", "detail-2"])
    qtbot.waitUntil(lambda: len(window.player_window.opened) == 2)
    assert window.player_window.opened[-1][0]["vod"].vod_id == "detail-2"


def test_main_window_detail_field_link_click_opens_browser(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            self.opened: list[tuple[object, bool]] = []
            self.closed_to_main = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class RoutedPluginController(FakeStaticController):
        def load_categories(self):
            return []

        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="详情结果"),
                playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
                clicked_index=0,
                source_kind="plugin",
                source_key="plugin-1",
                source_mode="detail",
                source_vod_id=vod_id,
            )

    opened: list[str] = []
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    monkeypatch.setattr(main_window_module.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    controller = RoutedPluginController()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[{"id": "plugin-1", "title": "插件1", "controller": controller, "search_enabled": True}],
    )
    qtbot.addWidget(window)
    request = controller.build_request("detail-1")

    window.open_player(request)
    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1)

    session = window.player_window.opened[0][0]
    runner = session["detail_field_runner"]
    runner(session["playlist"][0], PlaybackDetailFieldAction(type="link", value="https://example.com"))

    assert opened == ["https://example.com"]


def test_main_window_bilibili_metadata_category_click_loads_builtin_bilibili_results(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            self.opened: list[tuple[object, bool]] = []
            self.closed_to_main = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class RoutedBilibiliController(FakeStaticController):
        def __init__(self) -> None:
            self.category_calls: list[tuple[str, int]] = []

        def load_categories(self):
            return [type("Category", (), {"type_id": "recommend", "type_name": "推荐", "filters": []})()]

        def load_items(self, category_id: str, page: int, filters=None):
            self.category_calls.append((category_id, page))
            return [VodItem(vod_id="bili-1", vod_name="UP视频")], 1

        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="B站详情"),
                playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
                clicked_index=0,
                source_kind="bilibili",
                source_mode="detail",
                source_vod_id=vod_id,
            )

    controller = RoutedBilibiliController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        bilibili_controller=controller,
        config=AppConfig(),
        show_bilibili_tab=True,
    )
    qtbot.addWidget(window)
    request = controller.build_request("BV1xx411c7mD")

    window.open_player(request)
    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1)

    session = window.player_window.opened[0][0]
    runner = session["detail_field_runner"]
    assert callable(runner)

    runner(
        session["playlist"][0],
        PlaybackDetailFieldAction(target="bilibili", type="category", value="up:378885845"),
    )

    qtbot.waitUntil(lambda: controller.category_calls == [("up:378885845", 1)])
    assert window.bilibili_page is not None
    qtbot.waitUntil(lambda: bool(window.bilibili_page.items) and window.bilibili_page.items[0].vod_name == "UP视频")
    assert window.nav_tabs.currentWidget() is window.bilibili_page


def test_main_window_async_restore_failure_resets_last_active_window(qtbot) -> None:
    class FailingBrowseController(FakeStaticController):
        def build_request_from_detail(self, vod_id: str):
            raise RuntimeError(f"failed to restore {vod_id}")

    saved = {"count": 0}
    config = AppConfig(
        last_active_window="player",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
    )
    window = MainWindow(
        browse_controller=FailingBrowseController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)

    window._start_restore_last_player()

    qtbot.waitUntil(lambda: config.last_active_window == "main")
    assert saved["count"] >= 1


def test_main_window_restore_last_player_routes_custom_live_to_live_controller(qtbot, monkeypatch) -> None:
    class RestoreBrowseController:
        def build_request_from_detail(self, vod_id: str):
            raise AssertionError(f"browse restore should not be used for {vod_id}")

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.opened: list[tuple[object, bool]] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class RestoreLiveController(FakeStaticController):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def build_request(self, vod_id: str):
            self.calls.append(vod_id)
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="自定义频道"),
                playlist=[PlayItem(title="直播线路", url="https://live.example/custom.m3u8")],
                clicked_index=0,
                source_kind="live",
                source_mode="custom",
                source_vod_id=vod_id,
                use_local_history=False,
            )

    controller = RestoreLiveController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
        last_playback_source="live",
        last_playback_mode="custom",
        last_playback_vod_id="custom-channel:9:channel-0",
        last_player_paused=True,
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        live_controller=controller,
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    assert controller.calls == ["custom-channel:9:channel-0"]
    assert window.player_window.opened[0][0]["vod"].vod_name == "自定义频道"
    assert window.player_window.opened[0][1] is True


def test_main_window_restore_last_player_prepares_emby_request_with_metadata_and_danmaku_factories(
    qtbot,
    monkeypatch,
) -> None:
    class RestoreBrowseController:
        def build_request_from_detail(self, vod_id: str):
            raise AssertionError(f"browse restore should not be used for {vod_id}")

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.opened: list[tuple[object, bool]] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class RestoreEmbyController(FakeStaticController):
        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="恢复的 Emby 条目"),
                playlist=[PlayItem(title="第4集", url="", vod_id="emby-ep-4")],
                clicked_index=0,
                source_kind="emby",
                source_mode="detail",
                source_vod_id=vod_id,
            )

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
        last_playback_source="emby",
        last_playback_mode="detail",
        last_playback_vod_id="vod-restore-1",
        last_player_paused=True,
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        emby_controller=RestoreEmbyController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
        metadata_hydrator_factory=lambda **kwargs: ("hydrator", kwargs["source_kind"], kwargs["vod"].vod_id),
        metadata_scrape_service_factory=lambda **kwargs: ("scraper", kwargs["source_kind"], kwargs["vod"].vod_id),
        danmaku_controller_factory=lambda **kwargs: ("danmaku", kwargs["source_kind"], kwargs["vod"].vod_id),
        metadata_binding_repository="binding-repo",
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    opened_session, paused = window.player_window.opened[0]
    assert paused is True
    assert opened_session["metadata_hydrator"] == ("hydrator", "emby", "vod-restore-1")
    assert opened_session["metadata_scrape_service"] == ("scraper", "emby", "vod-restore-1")
    assert opened_session["danmaku_controller"] == ("danmaku", "emby", "vod-restore-1")
    assert opened_session["metadata_binding_repository"] == "binding-repo"


def test_main_window_restore_last_player_reloads_cached_plugin_danmaku(qtbot, monkeypatch, tmp_path) -> None:
    class FakeSpider:
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "玄界之门3D版",
                        "vod_pic": "poster-detail",
                        "vod_play_from": "默认线",
                        "vod_play_url": "第1集$/play/1",
                    }
                ]
            }

        def playerContent(self, flag, id, vipFlags):
            return {"parse": 0, "url": f"https://stream.example{id}.m3u8", "header": {"Referer": "https://site.example"}}

        def danmaku(self):
            return True

    class FakeDanmakuVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 150

        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers=None) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self):
            return []

        def audio_tracks(self):
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return False

        def supports_subtitle_ass_override(self) -> bool:
            return False

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    class RecordingRestorePlayerWindow(PlayerWindow):
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            super().__init__(controller, config, save_config, **kwargs)
            self.video = FakeDanmakuVideo()

    class FakeApiClient:
        def get_history(self, key: str):
            return None

        def save_history(self, payload: dict) -> None:
            return None

    class FakeParserService:
        def parsers(self):
            return []

        def resolve(self, _flag, _url, preferred_key=""):
            return type(
                "Resolved",
                (),
                {"url": "https://media.example/demo.m3u8", "headers": {"Referer": "https://site.example"}},
            )()

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(spider_controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(spider_controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        spider_controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        spider_controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingRestorePlayerWindow)
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>'

    class FirstDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            return xml_text

    class SecondDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

        def search_danmu(self, name: str, reg_src: str = ""):
            return []

        def resolve_danmu(self, page_url: str) -> str:
            raise AssertionError("restore should use cached danmaku xml")

    first_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=FirstDanmakuService(),
    )
    first_request = first_controller.build_request("/detail/1")
    assert first_request.playback_loader is not None
    seeded_item = first_request.playlist[0]
    first_request.playback_loader(seeded_item)
    seeded_item.danmaku_search_query = "玄界之门 1集"
    seeded_item.danmaku_search_query_overridden = True
    first_controller.refresh_danmaku_sources(seeded_item, query_override="玄界之门 1集", force_refresh=True)
    first_controller.switch_danmaku_source(seeded_item, "https://v.qq.com/demo")

    second_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=SecondDanmakuService(),
    )
    player_controller = PlayerController(FakeApiClient())
    config = AppConfig(
        last_active_window="player",
        last_playback_source="plugin",
        last_playback_source_key="plugin-1",
        last_playback_mode="detail",
        last_playback_vod_id="/detail/1",
        last_player_paused=True,
    )
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=player_controller,
        config=config,
        spider_plugins=[{"id": "plugin-1", "title": "插件一", "controller": second_controller, "search_enabled": False}],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    _spin_until(lambda: len(window.player_window.video.loaded_danmaku_paths) == 1)
    assert window.player_window.danmaku_combo.currentText() == "弹幕"


def test_main_window_restore_last_player_reloads_cached_direct_parse_danmaku(qtbot, monkeypatch, tmp_path) -> None:
    class FakeDanmakuVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 150

        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers=None) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self):
            return []

        def audio_tracks(self):
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return False

        def supports_subtitle_ass_override(self) -> bool:
            return False

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    class RecordingRestorePlayerWindow(PlayerWindow):
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            super().__init__(controller, config, save_config, **kwargs)
            self.video = FakeDanmakuVideo()

    class FakeApiClient:
        def get_history(self, key: str):
            return None

        def save_history(self, payload: dict) -> None:
            return None

    class FakeParserService:
        def parsers(self):
            return []

        def resolve(self, _flag, _url, preferred_key=""):
            return type(
                "Resolved",
                (),
                {"url": "https://media.example/demo.m3u8", "headers": {"Referer": "https://site.example"}},
            )()

    source_url = "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html"
    detail_payload = {
        "vod_form": source_url,
        "vod_title": "剑来 第二季",
        "vod_episodes": [
            {"name": "第10话", "url": source_url},
        ],
    }
    xml_payload = {
        "code": 23,
        "name": "demo",
        "danmuku": [
            [42.741, "right", "#00CD00", "1205421", "666", "03-15 15:47", "25px"],
        ],
    }

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(direct_parse_danmaku_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(direct_parse_danmaku_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingRestorePlayerWindow)

    seed_window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(preferred_parse_key="jx1"),
        plugin_manager=FakePluginManager(),
        playback_parser_service=FakeParserService(),
        direct_parse_detail_loader=lambda _url: detail_payload,
        direct_parse_danmaku_loader=lambda _url: xml_payload,
    )
    first_request = seed_window._build_direct_parse_request(source_url)
    assert first_request.playback_loader is not None
    assert first_request.danmaku_controller is not None
    seeded_item = first_request.playlist[0]
    first_request.playback_loader(seeded_item)
    first_request.danmaku_controller.switch_danmaku_source(seeded_item, source_url)

    restore_window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=PlayerController(FakeApiClient()),
        config=AppConfig(
            last_active_window="player",
            last_playback_source="direct_parse",
            last_playback_mode="parse",
            last_playback_vod_id=source_url,
            last_player_paused=True,
            preferred_parse_key="jx1",
        ),
        plugin_manager=FakePluginManager(),
        playback_parser_service=FakeParserService(),
        direct_parse_detail_loader=lambda _url: detail_payload,
        direct_parse_danmaku_loader=lambda _url: (_ for _ in ()).throw(AssertionError("restore should use cached danmaku xml")),
    )
    qtbot.addWidget(restore_window)

    restored = restore_window.restore_last_player()

    assert restored is restore_window.player_window
    _spin_until(lambda: len(restore_window.player_window.video.loaded_danmaku_paths) == 1)
    assert restore_window.player_window.danmaku_combo.currentText() == "弹幕"


def test_main_window_reopen_plugin_item_from_list_reloads_cached_danmaku(qtbot, monkeypatch, tmp_path) -> None:
    class FakeSpider:
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "玄界之门3D版",
                        "vod_pic": "poster-detail",
                        "vod_play_from": "默认线",
                        "vod_play_url": "第1集$/play/1",
                    }
                ]
            }

        def playerContent(self, flag, id, vipFlags):
            return {"parse": 0, "url": f"https://stream.example{id}.m3u8", "header": {"Referer": "https://site.example"}}

        def danmaku(self):
            return True

    class FakeDanmakuVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 150

        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers=None) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def pause(self) -> None:
            return None

        def subtitle_tracks(self):
            return []

        def audio_tracks(self):
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return False

        def supports_subtitle_ass_override(self) -> bool:
            return False

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    class RecordingRestorePlayerWindow(PlayerWindow):
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            super().__init__(controller, config, save_config, **kwargs)
            self.video = FakeDanmakuVideo()

    class FakeApiClient:
        def get_history(self, key: str):
            return None

        def save_history(self, payload: dict) -> None:
            return None

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(spider_controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(spider_controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        spider_controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        spider_controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingRestorePlayerWindow)
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>'

    class FirstDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            return xml_text

    class SecondDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

        def search_danmu(self, name: str, reg_src: str = ""):
            return []

        def resolve_danmu(self, page_url: str) -> str:
            raise AssertionError("reopen from list should use cached danmaku xml")

    first_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=FirstDanmakuService(),
    )
    first_request = first_controller.build_request("/detail/1")
    assert first_request.playback_loader is not None
    seeded_item = first_request.playlist[0]
    first_request.playback_loader(seeded_item)
    seeded_item.danmaku_search_query = "玄界之门 1集"
    seeded_item.danmaku_search_query_overridden = True
    first_controller.refresh_danmaku_sources(seeded_item, query_override="玄界之门 1集", force_refresh=True)
    first_controller.switch_danmaku_source(seeded_item, "https://v.qq.com/demo")

    second_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=SecondDanmakuService(),
    )
    player_controller = PlayerController(FakeApiClient())
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=player_controller,
        config=AppConfig(),
        spider_plugins=[{"id": "plugin-1", "title": "插件一", "controller": second_controller, "search_enabled": False}],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)

    request = second_controller.build_request("/detail/1")
    window.open_player(request)
    _spin_until(lambda: window.player_window is not None and len(window.player_window.video.loaded_danmaku_paths) == 1)
    window.player_window._return_to_main()

    list_item = VodItem(vod_id="/detail/1", vod_name="玄界之门3D版", vod_pic="poster-list")
    window._open_spider_item(second_controller, "plugin-1", list_item)

    _spin_until(lambda: len(window.player_window.video.loaded_danmaku_paths) >= 2)
    assert window.player_window.session is not None
    assert window.player_window.session.playlist[window.player_window.current_index].danmaku_pending is False
    assert window.player_window.danmaku_combo.currentText() == "弹幕"


def test_main_window_reopen_plugin_item_from_list_uses_reg_src_cached_danmaku_without_selected_source_download(
    qtbot, monkeypatch, tmp_path,
) -> None:
    class FakeSpider:
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "玄界之门3D版",
                        "vod_pic": "poster-detail",
                        "vod_play_from": "默认线",
                        "vod_play_url": "第1集$/play/1",
                    }
                ]
            }

        def playerContent(self, flag, id, vipFlags):
            return {"parse": 0, "url": f"https://stream.example{id}.m3u8", "header": {"Referer": "https://site.example"}}

        def danmaku(self):
            return True

    class FakeDanmakuVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 150

        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers=None) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def pause(self) -> None:
            return None

        def subtitle_tracks(self):
            return []

        def audio_tracks(self):
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return False

        def supports_subtitle_ass_override(self) -> bool:
            return False

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    class RecordingRestorePlayerWindow(PlayerWindow):
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            super().__init__(controller, config, save_config, **kwargs)
            self.video = FakeDanmakuVideo()

    class FakeApiClient:
        def get_history(self, key: str):
            return None

        def save_history(self, payload: dict) -> None:
            return None

    class FirstDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            return xml_text

    class SearchOnlyDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            raise AssertionError("selected source download should not run when reg_src cache already exists")

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(spider_controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(spider_controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        spider_controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        spider_controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingRestorePlayerWindow)
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>'

    first_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=FirstDanmakuService(),
    )
    request = first_controller.build_request("/detail/1")
    assert request.playback_loader is not None
    seeded_item = request.playlist[0]
    request.playback_loader(seeded_item)
    first_controller.refresh_danmaku_sources(seeded_item, playlist=request.playlist, force_refresh=True)
    danmaku_cache_module.save_cached_danmaku_xml(seeded_item.danmaku_search_query, seeded_item.vod_id, xml_text)

    second_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=SearchOnlyDanmakuService(),
    )

    player_controller = PlayerController(FakeApiClient())
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=player_controller,
        config=AppConfig(),
        spider_plugins=[{"id": "plugin-1", "title": "插件一", "controller": second_controller, "search_enabled": False}],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)

    first_request = first_controller.build_request("/detail/1")
    window.open_player(first_request)
    _spin_until(lambda: _player_window_has_active_danmaku(window))
    previous_session = window.player_window.session
    window.player_window._return_to_main()

    list_item = VodItem(vod_id="/detail/1", vod_name="玄界之门3D版", vod_pic="poster-list")
    window._open_spider_item(second_controller, "plugin-1", list_item)

    qtbot.waitUntil(
        lambda: (
            window.player_window is not None
            and window.player_window.session is not None
            and not getattr(window.player_window.session, "is_placeholder", False)
            and bool(window.player_window.session.playlist)
        )
    )
    _spin_until(
        lambda: (
            window.player_window is not None
            and window.player_window.session is not previous_session
            and _player_window_has_active_danmaku(window)
        )
    )
    assert window.player_window.session.playlist[window.player_window.current_index].danmaku_pending is False
    assert window.player_window.danmaku_combo.currentText() == "弹幕"


def test_main_window_reopen_direct_media_plugin_item_from_list_does_not_trigger_selected_source_restore(
    qtbot, monkeypatch, tmp_path,
) -> None:
    class FakeSpider:
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "玄界之门3D版",
                        "vod_pic": "poster-detail",
                        "vod_play_from": "默认线",
                        "vod_play_url": "第1集$https://stream.example/direct-1.m3u8",
                    }
                ]
            }

        def danmaku(self):
            return True

    class FakeDanmakuVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 150

        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers=None) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def pause(self) -> None:
            return None

        def subtitle_tracks(self):
            return []

        def audio_tracks(self):
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return False

        def supports_subtitle_ass_override(self) -> bool:
            return False

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    class RecordingRestorePlayerWindow(PlayerWindow):
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            super().__init__(controller, config, save_config, **kwargs)
            self.video = FakeDanmakuVideo()

    class FakeApiClient:
        def get_history(self, key: str):
            return None

        def save_history(self, payload: dict) -> None:
            return None

    class FirstDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            return xml_text

    class SearchOnlyDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            raise AssertionError("selected source restore should not run for fresh direct-media open")

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(spider_controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(spider_controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        spider_controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        spider_controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingRestorePlayerWindow)
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>'

    first_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=FirstDanmakuService(),
    )
    first_request = first_controller.build_request("/detail/1")
    seeded_item = first_request.playlist[0]
    first_controller.refresh_danmaku_sources(seeded_item, playlist=first_request.playlist, force_refresh=True)
    danmaku_cache_module.save_cached_danmaku_xml(seeded_item.danmaku_search_query, seeded_item.url, xml_text)

    second_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=SearchOnlyDanmakuService(),
    )
    player_controller = PlayerController(FakeApiClient())
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=player_controller,
        config=AppConfig(),
        spider_plugins=[{"id": "plugin-1", "title": "插件一", "controller": second_controller, "search_enabled": False}],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)

    first_open_request = first_controller.build_request("/detail/1")
    window.open_player(first_open_request)
    _spin_until(lambda: _player_window_has_active_danmaku(window))
    previous_session = window.player_window.session
    window.player_window._return_to_main()

    list_item = VodItem(vod_id="/detail/1", vod_name="玄界之门3D版", vod_pic="poster-list")
    window._open_spider_item(second_controller, "plugin-1", list_item)

    qtbot.waitUntil(
        lambda: (
            window.player_window is not None
            and window.player_window.session is not None
            and not getattr(window.player_window.session, "is_placeholder", False)
            and bool(window.player_window.session.playlist)
        )
    )
    _spin_until(
        lambda: (
            window.player_window is not None
            and window.player_window.session is not previous_session
            and _player_window_has_active_danmaku(window)
        )
    )
    assert "恢复缓存弹幕失败" not in window.player_window.log_view.toPlainText()
    assert window.player_window.session.playlist[window.player_window.current_index].danmaku_pending is False
    assert window.player_window.danmaku_combo.currentText() == "弹幕"


def test_main_window_reopen_drive_plugin_item_from_list_reloads_cached_danmaku(
    qtbot, monkeypatch, tmp_path,
) -> None:
    class FakeSpider:
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "网盘剧集",
                        "vod_pic": "poster-detail",
                        "vod_play_from": "百度",
                        "vod_play_url": "百度$https://pan.baidu.com/s/fake-share",
                    }
                ]
            }

        def danmaku(self):
            return True

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.baidu.com/s/fake-share"
        return {
            "list": [
                {
                    "vod_id": "drive-1",
                    "vod_name": "百度资源",
                    "items": [
                        {
                            "title": "10.mp4(1.31 GB)",
                            "url": "http://m/10.mp4",
                            "path": "/网盘剧集/10.mp4",
                            "size": 1400000000,
                        }
                    ],
                }
            ]
        }

    class FakeDanmakuVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 150

        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers=None) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def pause(self) -> None:
            return None

        def subtitle_tracks(self):
            return []

        def audio_tracks(self):
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return False

        def supports_subtitle_ass_override(self) -> bool:
            return False

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    class RecordingRestorePlayerWindow(PlayerWindow):
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            super().__init__(controller, config, save_config, **kwargs)
            self.video = FakeDanmakuVideo()

    class FakeApiClient:
        def get_history(self, key: str):
            return None

        def save_history(self, payload: dict) -> None:
            return None

    class FirstDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            return [type("Item", (), {"provider": "tencent", "name": name, "url": "https://v.qq.com/demo"})()]

        def resolve_danmu(self, page_url: str) -> str:
            return xml_text

    class SecondDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            raise AssertionError("drive reopen should hit cached danmaku without re-search")

        def resolve_danmu(self, page_url: str) -> str:
            raise AssertionError("drive reopen should hit cached danmaku without re-download")

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(spider_controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(spider_controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        spider_controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        spider_controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingRestorePlayerWindow)
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>'

    first_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="网盘剧集",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=FirstDanmakuService(),
    )
    first_request = first_controller.build_request("/detail/drive")
    assert first_request.playback_loader is not None
    first_result = first_request.playback_loader(first_request.playlists[0][0])
    assert first_result is not None
    drive_item = first_result.replacement_playlist[0]
    first_request.playback_loader(drive_item)
    qtbot.waitUntil(lambda: drive_item.danmaku_pending is False and bool(drive_item.danmaku_xml))

    second_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="网盘剧集",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=SecondDanmakuService(),
    )
    player_controller = PlayerController(FakeApiClient())
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=player_controller,
        config=AppConfig(),
        spider_plugins=[{"id": "plugin-1", "title": "插件一", "controller": second_controller, "search_enabled": False}],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)

    first_open_request = first_controller.build_request("/detail/drive")
    window.open_player(first_open_request)
    _spin_until(lambda: window.player_window is not None and len(window.player_window.video.loaded_danmaku_paths) == 1)
    window.player_window._return_to_main()

    list_item = VodItem(vod_id="/detail/drive", vod_name="网盘剧集", vod_pic="poster-list")
    window._open_spider_item(second_controller, "plugin-1", list_item)

    qtbot.waitUntil(
        lambda: (
            window.player_window is not None
            and window.player_window.session is not None
            and not getattr(window.player_window.session, "is_placeholder", False)
            and bool(window.player_window.session.playlist)
        )
    )
    _spin_until(lambda: len(window.player_window.video.loaded_danmaku_paths) == 2)
    assert "弹幕搜索中" not in window.player_window.log_view.toPlainText()
    assert window.player_window.session.playlist[window.player_window.current_index].danmaku_pending is False
    assert window.player_window.danmaku_combo.currentText() == "弹幕"


def test_main_window_reopen_drive_plugin_item_from_list_after_closing_player_reloads_cached_danmaku(
    qtbot, monkeypatch, tmp_path,
) -> None:
    class FakeSpider:
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "网盘剧集",
                        "vod_pic": "poster-detail",
                        "vod_play_from": "百度",
                        "vod_play_url": "百度$https://pan.baidu.com/s/fake-share",
                    }
                ]
            }

        def danmaku(self):
            return True

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.baidu.com/s/fake-share"
        return {
            "list": [
                {
                    "vod_id": "drive-1",
                    "vod_name": "百度资源",
                    "items": [
                        {
                            "title": "10.mp4(1.31 GB)",
                            "url": "http://m/10.mp4",
                            "path": "/网盘剧集/10.mp4",
                            "size": 1400000000,
                        }
                    ],
                }
            ]
        }

    class FakeDanmakuVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 150

        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers=None) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def pause(self) -> None:
            return None

        def subtitle_tracks(self):
            return []

        def audio_tracks(self):
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return False

        def supports_subtitle_ass_override(self) -> bool:
            return False

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    class RecordingRestorePlayerWindow(PlayerWindow):
        def __init__(self, controller, config, save_config, **kwargs) -> None:
            super().__init__(controller, config, save_config, **kwargs)
            self.video = FakeDanmakuVideo()

    class FakeApiClient:
        def get_history(self, key: str):
            return None

        def save_history(self, payload: dict) -> None:
            return None

    class FirstDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            return [type("Item", (), {"provider": "tencent", "name": name, "url": "https://v.qq.com/demo"})()]

        def resolve_danmu(self, page_url: str) -> str:
            return xml_text

    class SecondDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            raise AssertionError("drive reopen after close should hit cached danmaku without re-search")

        def resolve_danmu(self, page_url: str) -> str:
            raise AssertionError("drive reopen after close should hit cached danmaku without re-download")

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(spider_controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(spider_controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        spider_controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        spider_controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingRestorePlayerWindow)
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>'

    first_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="网盘剧集",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=FirstDanmakuService(),
    )
    first_request = first_controller.build_request("/detail/drive")
    assert first_request.playback_loader is not None
    first_result = first_request.playback_loader(first_request.playlists[0][0])
    assert first_result is not None
    drive_item = first_result.replacement_playlist[0]
    first_request.playback_loader(drive_item)
    qtbot.waitUntil(lambda: drive_item.danmaku_pending is False and bool(drive_item.danmaku_xml))

    second_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="网盘剧集",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=SecondDanmakuService(),
    )
    player_controller = PlayerController(FakeApiClient())
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=player_controller,
        config=AppConfig(),
        spider_plugins=[{"id": "plugin-1", "title": "插件一", "controller": second_controller, "search_enabled": False}],
        plugin_manager=WidthAwarePluginManager(),
    )
    qtbot.addWidget(window)

    first_open_request = first_controller.build_request("/detail/drive")
    window.open_player(first_open_request)
    _spin_until(lambda: window.player_window is not None and len(window.player_window.video.loaded_danmaku_paths) == 1)
    player_window = window.player_window
    player_window.close()
    qtbot.waitUntil(lambda: window.player_window is player_window and player_window.isHidden() and window.isVisible())

    list_item = VodItem(vod_id="/detail/drive", vod_name="网盘剧集", vod_pic="poster-list")
    window._open_spider_item(second_controller, "plugin-1", list_item)

    qtbot.waitUntil(
        lambda: (
            window.player_window is not None
            and window.player_window.session is not None
            and not getattr(window.player_window.session, "is_placeholder", False)
            and bool(window.player_window.session.playlist)
        )
    )
    _spin_until(lambda: len(window.player_window.video.loaded_danmaku_paths) == 2)
    assert "弹幕搜索中" not in window.player_window.log_view.toPlainText()
    assert window.player_window.session.playlist[window.player_window.current_index].danmaku_pending is False
    assert window.player_window.danmaku_combo.currentText() == "弹幕"


def test_main_window_async_restore_without_saved_request_resets_last_active_window(qtbot) -> None:
    saved = {"count": 0}
    config = AppConfig(last_active_window="player")
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)

    window._start_restore_last_player()

    qtbot.waitUntil(lambda: config.last_active_window == "main")
    assert saved["count"] >= 1


def test_main_window_async_restore_session_creation_failure_resets_last_active_window(qtbot) -> None:
    class RestoreBrowseController(FakeStaticController):
        def build_request_from_detail(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="Movie"),
                playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
                clicked_index=0,
                source_mode="detail",
                source_vod_id=vod_id,
            )

    class FailingPlayerController(FakePlayerController):
        def create_session(
            self,
            vod,
            playlist,
            clicked_index: int,
            playlists=None,
            playlist_index: int = 0,
            source_groups=None,
            source_group_index: int = 0,
            source_index: int = 0,
            source_kind: str = "",
            source_key: str = "",
            detail_resolver=None,
            resolved_vod_by_id=None,
            use_local_history=True,
            restore_history=False,
            playback_loader=None,
            async_playback_loader=False,
            detail_action_runner=None,
            detail_field_runner=None,
            metadata_hydrator=None,
            metadata_scrape_service=None,
            metadata_binding_repository=None,
            episode_title_enhancer=None,
            danmaku_controller=None,
            playback_progress_reporter=None,
            playback_stopper=None,
            playback_history_loader=None,
            playback_history_saver=None,
            initial_log_message="",
            is_placeholder=False,
        ):
            del (
                vod,
                playlist,
                clicked_index,
                playlists,
                playlist_index,
                source_groups,
                source_group_index,
                source_index,
                source_kind,
                source_key,
                detail_resolver,
                resolved_vod_by_id,
                use_local_history,
                restore_history,
                playback_loader,
                async_playback_loader,
                detail_action_runner,
                detail_field_runner,
                metadata_hydrator,
                metadata_scrape_service,
                metadata_binding_repository,
                episode_title_enhancer,
                danmaku_controller,
                playback_progress_reporter,
                playback_stopper,
                playback_history_loader,
                playback_history_saver,
                initial_log_message,
                is_placeholder,
            )
            raise RuntimeError("session failed")

    saved = {"count": 0}
    errors: list[str] = []
    config = AppConfig(
        last_active_window="player",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        history_controller=FakeStaticController(),
        player_controller=FailingPlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)
    window.show_error = errors.append

    window._start_restore_last_player()

    qtbot.waitUntil(lambda: config.last_active_window == "main")
    assert errors == ["session failed"]
    assert saved["count"] >= 1


def test_main_window_drops_closed_player_window_reference_when_returning_to_main(qtbot) -> None:
    class ClosedPlayerWindow:
        def __init__(self) -> None:
            self.session = None

    config = AppConfig(last_active_window="player")
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window.player_window = ClosedPlayerWindow()

    window._show_main_again()

    assert window.player_window is None
    assert config.last_active_window == "main"


def test_main_window_remaximizes_when_returning_from_player(qtbot, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    config = AppConfig(last_active_window="player", main_window_geometry=b"saved-geometry")
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window._main_window_was_maximized_before_player = True

    monkeypatch.setattr(main_window_module.QTimer, "singleShot", lambda _delay, callback: calls.append(("singleShot", callback)))
    monkeypatch.setattr(window, "showMaximized", lambda: calls.append(("showMaximized", None)))
    monkeypatch.setattr(window, "show", lambda: calls.append(("show", None)))
    monkeypatch.setattr(window, "restoreGeometry", lambda _geometry: calls.append(("restoreGeometry", None)) or True)

    window._show_main_again()

    assert ("restoreGeometry", None) in calls
    assert ("show", None) in calls
    assert ("showMaximized", None) in calls
    assert calls.index(("show", None)) < calls.index(("showMaximized", None))


def test_main_window_restores_in_memory_geometry_before_saved_geometry_when_returning_from_player(qtbot, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    config = AppConfig(last_active_window="player", main_window_geometry=b"saved-geometry")
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window._main_window_was_maximized_before_player = True
    window._main_window_geometry_before_player = main_window_module.QRect(40, 50, 800, 600)

    monkeypatch.setattr(main_window_module.QTimer, "singleShot", lambda _delay, callback: calls.append(("singleShot", callback)))
    monkeypatch.setattr(window, "_restore_saved_geometry", lambda *args, **kwargs: calls.append(("restore_saved_geometry", None)))
    monkeypatch.setattr(window, "setGeometry", lambda rect: calls.append(("setGeometry", rect)))
    monkeypatch.setattr(window, "showMaximized", lambda: calls.append(("showMaximized", None)))
    monkeypatch.setattr(window, "showNormal", lambda: calls.append(("showNormal", None)))

    window._show_main_again()

    assert ("restore_saved_geometry", None) not in calls
    assert ("setGeometry", main_window_module.QRect(40, 50, 800, 600)) in calls
    assert ("showMaximized", None) in calls
    assert ("showNormal", None) not in calls


def test_main_window_reapplies_saved_geometry_when_no_player_return_state(qtbot, monkeypatch) -> None:
    restore_calls: list[bytes] = []

    def fake_restore_geometry(self, geometry) -> bool:
        restore_calls.append(bytes(geometry.data()))
        return True

    monkeypatch.setattr(MainWindow, "restoreGeometry", fake_restore_geometry)
    config = AppConfig(last_active_window="player", main_window_geometry=b"saved-geometry")
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    restore_calls.clear()

    window._show_main_again()

    assert restore_calls == [b"saved-geometry"]


def test_main_window_remaximizes_when_saved_geometry_marks_maximized(qtbot, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    payload = {
        "x": 10,
        "y": 20,
        "width": 1280,
        "height": 720,
        "maximized": True,
    }
    config = AppConfig(
        last_active_window="player",
        main_window_geometry=(
            main_window_module._CUSTOM_MAIN_WINDOW_GEOMETRY_PREFIX
            + json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ),
    )
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window._main_window_was_maximized_before_player = False

    monkeypatch.setattr(main_window_module.QTimer, "singleShot", lambda _delay, callback: calls.append(("singleShot", callback)))
    monkeypatch.setattr(window, "showMaximized", lambda: calls.append(("showMaximized", None)))
    monkeypatch.setattr(window, "show", lambda: calls.append(("show", None)))
    monkeypatch.setattr(window, "setGeometry", lambda _rect: calls.append(("setGeometry", None)))

    window._show_main_again()

    assert ("setGeometry", None) in calls
    assert ("show", None) in calls
    assert ("showMaximized", None) in calls
    assert calls.index(("show", None)) < calls.index(("showMaximized", None))


def test_main_window_retries_maximize_on_next_event_loop_when_returning_from_player(qtbot, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    scheduled: list[tuple[int, object]] = []
    config = AppConfig(last_active_window="player", main_window_geometry=b"saved-geometry")
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window._main_window_was_maximized_before_player = True

    def fake_show_maximized() -> None:
        calls.append(("showMaximized", None))

    def fake_refresh() -> None:
        calls.append(("refresh", None))

    monkeypatch.setattr(main_window_module.QTimer, "singleShot", lambda delay, callback: scheduled.append((delay, callback)))
    monkeypatch.setattr(window, "showMaximized", fake_show_maximized)
    monkeypatch.setattr(window, "_refresh_main_window_layout", fake_refresh)
    monkeypatch.setattr(window, "show", lambda: calls.append(("show", None)))
    monkeypatch.setattr(window, "restoreGeometry", lambda _geometry: calls.append(("restoreGeometry", None)) or True)

    window._show_main_again()

    assert ("showMaximized", None) in calls
    assert (0, fake_show_maximized) in scheduled
    assert (0, fake_refresh) in scheduled


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_main_window_ignores_async_open_request_after_window_deletion(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.closed_to_main = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            return None

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    controller = AsyncOpenController()
    window = MainWindow(
        telegram_controller=controller,
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    destroyed = {"count": 0}
    window.destroyed.connect(lambda *_args: destroyed.__setitem__("count", destroyed["count"] + 1))

    item = VodItem(vod_id="vod-1", vod_name="Movie")
    window._handle_telegram_item_open_requested(item)
    qtbot.waitUntil(lambda: controller.calls == ["vod-1"], timeout=1000)

    window.deleteLater()
    qtbot.waitUntil(lambda: destroyed["count"] == 1, timeout=1000)

    controller.release()
    qtbot.wait(100)

    assert destroyed["count"] == 1


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_main_window_ignores_async_media_load_after_window_deletion(qtbot) -> None:
    controller = AsyncMediaController()
    window = MainWindow(
        live_controller=controller,
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    destroyed = {"count": 0}
    window.destroyed.connect(lambda *_args: destroyed.__setitem__("count", destroyed["count"] + 1))

    item = type("Item", (), {"vod_id": "folder-1", "vod_name": "Folder", "vod_tag": "folder"})()
    window._open_media_folder(window.live_page, controller, item)
    qtbot.waitUntil(lambda: controller.calls == ["folder-1"], timeout=1000)

    window.deleteLater()
    qtbot.waitUntil(lambda: destroyed["count"] == 1, timeout=1000)

    controller.release()
    qtbot.wait(100)

    assert destroyed["count"] == 1


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_main_window_ignores_async_restore_after_window_deletion(qtbot) -> None:
    controller = AsyncRestoreController()
    config = AppConfig(
        last_active_window="player",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
    )
    window = MainWindow(
        browse_controller=controller,
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
    )
    destroyed = {"count": 0}
    window.destroyed.connect(lambda *_args: destroyed.__setitem__("count", destroyed["count"] + 1))

    window._start_restore_last_player()
    qtbot.waitUntil(lambda: controller.calls == ["vod-1"], timeout=1000)

    window.deleteLater()
    qtbot.waitUntil(lambda: destroyed["count"] == 1, timeout=1000)

    controller.release()
    qtbot.wait(100)

    assert destroyed["count"] == 1


def test_main_window_prepares_metadata_hydrator_for_browse_request(qtbot) -> None:
    marker = object()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=SimpleNamespace(load_items=lambda: [], refresh=lambda: None),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        save_config=lambda: None,
        metadata_hydrator_factory=lambda **_: marker,
    )
    qtbot.addWidget(window)
    request = OpenPlayerRequest(
        vod=VodItem(vod_id="v1", vod_name="Movie"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        clicked_index=0,
        source_kind="browse",
        source_mode="detail",
        source_vod_id="v1",
    )

    prepared = window._prepare_request_for_open(request)

    assert prepared.metadata_hydrator is marker


def test_main_window_telegram_open_preserves_list_title_as_media_title(qtbot, monkeypatch) -> None:
    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            del controller, config, save_config
            self.closed_to_main = SimpleNamespace(connect=lambda _callback: None)
            self.opened: list[tuple[object, bool]] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class TelegramOpenController(FakeStaticController):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def build_request(self, vod_id: str):
            self.calls.append(vod_id)
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="【C】成丨何体统"),
                playlist=[PlayItem(title="正片", url="https://media.example/movie.m3u8")],
                clicked_index=0,
                source_kind="telegram",
                source_mode="detail",
                source_vod_id=vod_id,
            )

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    controller = TelegramOpenController()
    window = MainWindow(
        telegram_controller=controller,
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)

    window._handle_telegram_item_open_requested(VodItem(vod_id="vod-1", vod_name="成何体统 (2026)"))

    qtbot.waitUntil(lambda: controller.calls == ["vod-1"])
    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1)
    session = window.player_window.opened[0][0]

    assert session["vod"].vod_name == "【C】成丨何体统"
    assert session["playlist"][0].media_title == "成何体统 (2026)"


def test_main_window_telegram_page_item_open_signal_preserves_list_title(qtbot, monkeypatch) -> None:
    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            del controller, config, save_config
            self.closed_to_main = SimpleNamespace(connect=lambda _callback: None)
            self.opened: list[tuple[object, bool]] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class TelegramOpenController(FakeStaticController):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def build_request(self, vod_id: str):
            self.calls.append(vod_id)
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="【C】成丨何体统"),
                playlist=[PlayItem(title="正片", url="https://media.example/movie.m3u8")],
                clicked_index=0,
                source_kind="telegram",
                source_mode="detail",
                source_vod_id=vod_id,
            )

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    controller = TelegramOpenController()
    window = MainWindow(
        telegram_controller=controller,
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)

    window.telegram_page.item_open_requested.emit(VodItem(vod_id="vod-1", vod_name="成何体统 (2026)"))

    qtbot.waitUntil(lambda: controller.calls == ["vod-1"])
    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1)
    session = window.player_window.opened[0][0]

    assert session["playlist"][0].media_title == "成何体统 (2026)"


def test_main_window_restore_last_player_uses_saved_history_title_as_telegram_media_title(qtbot, monkeypatch) -> None:
    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            del controller, config, save_config
            self.closed_to_main = SimpleNamespace(connect=lambda _callback: None)
            self.opened: list[tuple[object, bool]] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class RestoreTelegramController(FakeStaticController):
        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="di|纸上|f|紫微(2026)"),
                playlist=[PlayItem(title="正片", url="https://media.example/movie.m3u8", media_title="di|纸上|f|紫微(2026)")],
                clicked_index=0,
                source_kind="telegram",
                source_mode="detail",
                source_vod_id=vod_id,
                use_local_history=False,
                playback_history_loader=lambda: HistoryRecord(
                    id=0,
                    key=vod_id,
                    vod_name="纸上紫微",
                    vod_pic="poster",
                    vod_remarks="正片",
                    episode=0,
                    episode_url="https://media.example/movie.m3u8",
                    position=45000,
                    opening=0,
                    ending=0,
                    speed=1.0,
                    create_time=1713206400000,
                    source_kind="telegram",
                ),
            )

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    window = MainWindow(
        telegram_controller=RestoreTelegramController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(
            last_active_window="player",
            last_playback_source="telegram",
            last_playback_mode="detail",
            last_playback_vod_id="vod-1",
            last_player_paused=True,
        ),
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    session = window.player_window.opened[0][0]
    assert session["playlist"][0].media_title == "纸上紫微"


def test_main_window_open_history_detail_uses_saved_history_title_as_telegram_media_title(qtbot, monkeypatch) -> None:
    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            del controller, config, save_config
            self.closed_to_main = SimpleNamespace(connect=lambda _callback: None)
            self.opened: list[tuple[object, bool]] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class TelegramHistoryController(FakeStaticController):
        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="di|纸上|f|紫微(2026)"),
                playlist=[PlayItem(title="正片", url="https://media.example/movie.m3u8", media_title="di|纸上|f|紫微(2026)")],
                clicked_index=0,
                source_kind="telegram",
                source_mode="detail",
                source_vod_id=vod_id,
                use_local_history=False,
                playback_history_loader=lambda: HistoryRecord(
                    id=0,
                    key=vod_id,
                    vod_name="纸上紫微",
                    vod_pic="poster",
                    vod_remarks="正片",
                    episode=0,
                    episode_url="https://media.example/movie.m3u8",
                    position=45000,
                    opening=0,
                    ending=0,
                    speed=1.0,
                    create_time=1713206400000,
                    source_kind="telegram",
                ),
            )

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    scrape_service = object()
    window = MainWindow(
        telegram_controller=TelegramHistoryController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        metadata_scrape_service_factory=lambda **_: scrape_service,
    )
    qtbot.addWidget(window)

    window.open_history_detail(
        HistoryRecord(
            id=0,
            key="vod-1",
            vod_name="纸上紫微",
            vod_pic="poster",
            vod_remarks="正片",
            episode=0,
            episode_url="https://media.example/movie.m3u8",
            position=45000,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1713206400000,
            source_kind="telegram",
            source_name="电报影视",
        )
    )

    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1)

    session = window.player_window.opened[0][0]
    assert session["playlist"][0].media_title == "纸上紫微"
    assert session["metadata_scrape_service"] is scrape_service


def test_main_window_prepares_episode_title_enhancer_for_browse_request(qtbot) -> None:
    marker = object()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=SimpleNamespace(load_items=lambda: [], refresh=lambda: None),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        save_config=lambda: None,
        episode_title_enhancer_factory=lambda **_: marker,
    )
    qtbot.addWidget(window)
    request = OpenPlayerRequest(
        vod=VodItem(vod_id="v1", vod_name="Movie"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        clicked_index=0,
        source_kind="browse",
        source_mode="detail",
        source_vod_id="v1",
    )

    prepared = window._prepare_request_for_open(request)

    assert prepared.episode_title_enhancer is marker


def test_main_window_does_not_prepare_metadata_for_loading_placeholder(qtbot) -> None:
    hydrator_calls: list[str] = []
    hydrator = object()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=SimpleNamespace(load_items=lambda: [], refresh=lambda: None),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        save_config=lambda: None,
        metadata_hydrator_factory=lambda **kwargs: hydrator_calls.append(kwargs["vod"].vod_name) or hydrator,
    )
    qtbot.addWidget(window)
    placeholder = OpenPlayerRequest(
        vod=VodItem(
            vod_id="magnet:?xt=urn:btih:f7a602d85036999a1b66025a359a1c647279a51f",
            vod_name="magnet:?xt=urn:btih:f7a602d85036999a1b66025a359a1c647279a51f",
        ),
        playlist=[],
        clicked_index=0,
        source_kind="browse",
        source_mode="detail",
        source_vod_id="magnet:?xt=urn:btih:f7a602d85036999a1b66025a359a1c647279a51f",
        initial_log_message="正在加载详情...",
        is_placeholder=True,
    )
    detail_request = OpenPlayerRequest(
        vod=VodItem(vod_id="detail-1", vod_name="低智商犯罪"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        clicked_index=0,
        source_kind="browse",
        source_mode="detail",
        source_vod_id=placeholder.source_vod_id,
    )

    prepared_placeholder = window._prepare_request_for_open(placeholder)
    prepared_detail = window._prepare_request_for_open(detail_request)

    assert prepared_placeholder.metadata_hydrator is None
    assert prepared_detail.metadata_hydrator is hydrator
    assert hydrator_calls == ["低智商犯罪"]


@pytest.mark.parametrize("source_kind", ["telegram", "emby", "jellyfin", "feiniu"])
def test_main_window_prepares_metadata_and_danmaku_for_supported_media_sources(qtbot, source_kind: str) -> None:
    hydrator = object()
    scrape_service = object()
    danmaku_controller = object()
    episode_title_enhancer = object()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=SimpleNamespace(load_items=lambda: [], refresh=lambda: None),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        save_config=lambda: None,
        metadata_hydrator_factory=lambda **_: hydrator,
        metadata_scrape_service_factory=lambda **_: scrape_service,
        danmaku_controller_factory=lambda **_: danmaku_controller,
        episode_title_enhancer_factory=lambda **_: episode_title_enhancer,
    )
    qtbot.addWidget(window)
    request = OpenPlayerRequest(
        vod=VodItem(vod_id="v1", vod_name="Movie"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        clicked_index=0,
        source_kind=source_kind,
        source_mode="detail",
        source_vod_id="v1",
    )

    prepared = window._prepare_request_for_open(request)

    assert prepared.metadata_hydrator is hydrator
    assert prepared.metadata_scrape_service is scrape_service
    assert prepared.danmaku_controller is danmaku_controller
    assert prepared.episode_title_enhancer is episode_title_enhancer


def test_main_window_does_not_backfill_plugin_metadata_hydrator_but_keeps_scrape_service(qtbot) -> None:
    hydrator = object()
    scrape_service = object()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=SimpleNamespace(load_items=lambda: [], refresh=lambda: None),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        save_config=lambda: None,
        metadata_hydrator_factory=lambda **_: hydrator,
        metadata_scrape_service_factory=lambda **_: scrape_service,
    )
    qtbot.addWidget(window)
    request = OpenPlayerRequest(
        vod=VodItem(vod_id="v1", vod_name="Plugin Movie"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        clicked_index=0,
        source_kind="plugin",
        source_key="plugin.demo",
        source_mode="detail",
        source_vod_id="v1",
    )

    prepared = window._prepare_request_for_open(request)

    assert prepared.metadata_hydrator is None
    assert prepared.metadata_scrape_service is scrape_service


def test_main_window_does_not_backfill_youtube_metadata_enhancement(qtbot) -> None:
    hydrator = object()
    scrape_service = object()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=SimpleNamespace(load_items=lambda: [], refresh=lambda: None),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        save_config=lambda: None,
        metadata_hydrator_factory=lambda **_: hydrator,
        metadata_scrape_service_factory=lambda **_: scrape_service,
    )
    qtbot.addWidget(window)
    request = OpenPlayerRequest(
        vod=VodItem(vod_id="yt:video:abc123", vod_name="YouTube Video"),
        playlist=[PlayItem(title="正片", url="")],
        clicked_index=0,
        source_kind="youtube",
        source_mode="detail",
        source_vod_id="yt:video:abc123",
    )

    prepared = window._prepare_request_for_open(request)

    assert prepared.metadata_hydrator is None
    assert prepared.metadata_scrape_service is None
