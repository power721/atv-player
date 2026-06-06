import time
from types import SimpleNamespace

from atv_player.controllers.telegram_search_controller import TelegramSearchController
from atv_player.models import DoubanCategory
from atv_player.telegram_media import (
    TelegramChatInfo,
    TelegramMediaRepository,
    TelegramMediaService,
    TelegramResource,
    _resources_from_message,
)


class FakeApiClient:
    def __init__(self) -> None:
        self.category_payload = {"class": []}
        self.items_payload = {"list": [], "total": 0}
        self.search_payload = {"list": [], "total": 0}
        self.detail_payload = {"list": []}
        self.item_calls: list[tuple[str, int]] = []
        self.search_calls: list[tuple[str, int]] = []
        self.detail_calls: list[str] = []
        self.get_detail_calls: list[str] = []

    def list_telegram_search_categories(self) -> dict:
        return self.category_payload

    def list_telegram_search_items(self, category_id: str, page: int) -> dict:
        self.item_calls.append((category_id, page))
        return self.items_payload

    def search_telegram_items(self, keyword: str, page: int) -> dict:
        self.search_calls.append((keyword, page))
        return self.search_payload

    def get_telegram_search_detail(self, vod_id: str) -> dict:
        self.detail_calls.append(vod_id)
        return self.detail_payload

    def get_detail(self, vod_id: str) -> dict:
        self.get_detail_calls.append(vod_id)
        return {
            "list": [
                {
                    "vod_id": vod_id,
                    "vod_name": f"Resolved {vod_id}",
                    "vod_play_url": f"http://m/{vod_id}.m3u8",
                    "items": [
                        {"title": f"Resolved {vod_id}", "url": f"http://m/{vod_id}.m3u8", "vod_id": vod_id},
                    ],
                }
            ]
        }


class FakeLocalMediaService:
    configured = True

    def __init__(
        self,
        resources: list[TelegramResource],
        chats: list[TelegramChatInfo] | None = None,
        sync_resources: dict[int, list[TelegramResource]] | None = None,
        index_page_batches: list[tuple[list[TelegramResource], int, int]] | None = None,
    ) -> None:
        self.resources = resources
        self.chats = chats or [
            TelegramChatInfo(id=-100, title="私密频道", kind="channel", enabled=True, browse_enabled=True)
        ]
        self.sync_resources = sync_resources or {}
        self.index_page_batches = list(index_page_batches or [])
        self.sync_calls: list[tuple[int, int | None]] = []
        self.index_calls: list[tuple[int, int | None]] = []
        self.index_page_calls: list[tuple[int, int | None, int]] = []

    def list_chats(self) -> list[TelegramChatInfo]:
        return list(self.chats)

    def search(self, keyword: str, *, limit: int = 30, offset: int = 0) -> list[TelegramResource]:
        searchable_chat_ids = {chat.id for chat in self.chats if chat.enabled and not chat.username}
        if not keyword:
            matches = [resource for resource in self.resources if resource.chat_id in searchable_chat_ids]
        else:
            matches = [
                resource
                for resource in self.resources
                if resource.chat_id in searchable_chat_ids and (keyword in resource.title or keyword in resource.text)
            ]
        return matches[offset: offset + limit]

    def search_chat(
        self,
        chat_id: int,
        keyword: str,
        *,
        limit: int = 30,
        offset: int = 0,
    ) -> list[TelegramResource]:
        matches = [
            resource
            for resource in self.resources
            if resource.chat_id == chat_id
            and (not keyword or keyword in resource.title or keyword in resource.text)
        ]
        return matches[offset: offset + limit]

    def browse_recent(self, *, limit: int = 30, offset: int = 0) -> list[TelegramResource]:
        browse_chat_ids = {chat.id for chat in self.chats if chat.browse_enabled}
        matches = [resource for resource in self.resources if resource.chat_id in browse_chat_ids]
        return matches[offset: offset + limit]

    def browse_chat(self, chat_id: int, *, limit: int = 30, offset: int = 0) -> list[TelegramResource]:
        matches = [resource for resource in self.resources if resource.chat_id == chat_id]
        return matches[offset: offset + limit]

    def sync_chat(self, chat_id: int, *, limit: int | None = 500) -> int:
        self.sync_calls.append((chat_id, limit))
        resources = self.sync_resources.pop(chat_id, [])
        self.resources.extend(resources)
        return len(resources)

    def index_chat_recent(self, chat_id: int, *, limit: int | None = 1000) -> int:
        self.index_calls.append((chat_id, limit))
        resources = self.sync_resources.pop(chat_id, [])
        self.resources.extend(resources)
        return len(resources)

    def index_chat_recent_page(
        self,
        chat_id: int,
        *,
        limit: int | None = 30,
        offset_id: int = 0,
    ):
        self.index_page_calls.append((chat_id, limit, offset_id))
        if self.index_page_batches:
            resources, message_count, oldest_msg_id = self.index_page_batches.pop(0)
        else:
            resources = self.sync_resources.pop(chat_id, []) if offset_id == 0 else []
            message_count = len(resources)
            oldest_msg_id = min((resource.msg_id for resource in resources), default=0)
        self.resources.extend(resources)
        return SimpleNamespace(
            resource_count=len(resources),
            message_count=message_count,
            oldest_msg_id=oldest_msg_id,
        )

    def get_media(self, chat_id: int, msg_id: int) -> TelegramResource | None:
        return next(
            (
                resource
                for resource in self.resources
                if resource.kind == "media" and resource.chat_id == chat_id and resource.msg_id == msg_id
            ),
            None,
        )


def test_load_categories_inserts_recommendation_first() -> None:
    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": "XiangxiuNBB", "type_name": "香秀"},
            {"type_id": "Movie", "type_name": "电影"},
        ]
    }
    controller = TelegramSearchController(api)

    categories = controller.load_categories()

    assert categories == [
        DoubanCategory(type_id="0", type_name="推荐"),
        DoubanCategory(type_id="XiangxiuNBB", type_name="香秀"),
        DoubanCategory(type_id="Movie", type_name="电影"),
    ]


def test_load_items_uses_recommendation_endpoint_without_page_param() -> None:
    api = FakeApiClient()
    controller = TelegramSearchController(api)

    controller.load_items("0", page=1)
    controller.load_items("XiangxiuNBB", page=3)

    assert api.item_calls == [("0", 1), ("XiangxiuNBB", 3)]


def test_telegram_search_controller_ignores_optional_filters_argument() -> None:
    api = FakeApiClient()
    controller = TelegramSearchController(api)

    controller.load_items("Movie", page=1, filters={"status": "1"})

    assert api.item_calls[-1] == ("Movie", 1)


def test_search_items_maps_search_payload() -> None:
    api = FakeApiClient()
    api.search_payload = {
        "list": [
            {
                "vod_id": "https://pan.quark.cn/s/demo",
                "vod_name": "黑袍纠察队",
                "vod_pic": "poster.jpg",
                "vod_remarks": "4K",
            }
        ],
        "total": 31,
    }
    controller = TelegramSearchController(api)

    items, total = controller.search_items("黑袍纠察队", page=1)

    assert api.search_calls == [("黑袍纠察队", 1)]
    assert total == 2
    assert items[0].vod_id == "https://pan.quark.cn/s/demo"
    assert items[0].vod_name == "黑袍纠察队"
    assert items[0].vod_pic == "poster.jpg"
    assert items[0].vod_remarks == "4K"


def test_configured_local_service_does_not_replace_backend_telegram_when_not_preferred() -> None:
    api = FakeApiClient()
    api.category_payload = {"class": [{"type_id": "Movie", "type_name": "电影"}]}
    service = FakeLocalMediaService(
        [
            TelegramResource(
                key="media:-100:1",
                kind="media",
                chat_id=-100,
                msg_id=1,
                chat_title="私密频道",
                title="Local.Movie.mkv",
            )
        ]
    )
    controller = TelegramSearchController(api, local_media_service=service, prefer_local_media=False)

    categories = controller.load_categories()

    assert [category.type_id for category in categories] == ["0", "Movie"]


def test_search_items_uses_pagecount_when_total_is_missing() -> None:
    api = FakeApiClient()
    api.search_payload = {"list": [], "pagecount": 3}
    controller = TelegramSearchController(api)

    _items, total = controller.search_items("黑袍纠察队", page=2)

    assert total == 3


def test_search_items_returns_pagecount_and_total_when_both_are_available() -> None:
    api = FakeApiClient()
    api.search_payload = {
        "list": [{"vod_id": "1", "vod_name": "黑袍纠察队"}],
        "pagecount": 2,
        "total": 31,
    }
    controller = TelegramSearchController(api)

    _items, total = controller.search_items("黑袍纠察队", page=1)

    assert total == 2
    assert total.pagecount == 2
    assert total.total == 31


def test_build_request_from_detail_uses_folder_playback_resolution_pattern() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "1$91792$1",
                "vod_name": "第 5 季 - 2160p WEB-DL HDR10+ H265 DDP 5.1",
                "vod_pic": "http://192.168.50.60:4567/list.png",
                "vod_play_url": (
                    "S05E01 - 第 1 集 - 2160p WEB-DL HDR10+ H265 DDP 5.1.mkv(8.43 GB)$1@91793@0@0#"
                    "S05E02 - 第 2 集 - 2160p WEB-DL HDR10+ H265 DDP 5.1.mkv(8.27 GB)$1@91794@0@1#"
                    "S05E03 - 第 3 集 - 2160p WEB HDR H265.mkv(8.69 GB)$1@91795@0@2"
                ),
                "vod_play_from": "丫仙女",
                "vod_content": "playlist folder",
                "path": "/我的夸克分享/temp/5@f518510ef92a@/Season 5/~playlist",
            }
        ]
    }
    controller = TelegramSearchController(api)

    source_vod_id = "https://pan.quark.cn/s/f518510ef92a"
    request = controller.build_request(source_vod_id)

    assert api.detail_calls == [source_vod_id]
    assert request.vod.vod_id == "1$91792$1"
    assert [item.title for item in request.playlist] == [
        "S05E01 - 第 1 集 - 2160p WEB-DL HDR10+ H265 DDP 5.1.mkv(8.43 GB)",
        "S05E02 - 第 2 集 - 2160p WEB-DL HDR10+ H265 DDP 5.1.mkv(8.27 GB)",
        "S05E03 - 第 3 集 - 2160p WEB HDR H265.mkv(8.69 GB)",
    ]
    assert [item.vod_id for item in request.playlist] == ["1@91793@0@0", "1@91794@0@1", "1@91795@0@2"]
    assert [item.url for item in request.playlist] == ["", "", ""]
    assert [item.media_title for item in request.playlist] == [request.vod.vod_name] * 3
    assert request.clicked_index == 0
    assert request.source_kind == "telegram"
    assert request.source_mode == "detail"
    assert request.source_vod_id == "1$91792$1"

    resolved = request.detail_resolver(request.playlist[1])

    assert api.get_detail_calls == ["1@91794@0@1"]
    assert resolved is not None
    assert resolved.vod_name == "Resolved 1@91794@0@1"


def test_build_request_exposes_local_telegram_history_hooks() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "1$91792$1",
                "vod_name": "剧集",
                "vod_play_url": "第1集$1@91793@0@0#第2集$1@91794@0@1",
            }
        ]
    }
    load_calls: list[str] = []
    save_calls: list[tuple[str, dict[str, object]]] = []
    controller = TelegramSearchController(
        api,
        playback_history_loader=lambda vod_id: load_calls.append(vod_id) or None,
        playback_history_saver=lambda vod_id, payload: save_calls.append((vod_id, payload)),
    )

    request = controller.build_request("https://pan.quark.cn/s/tg-detail-1")

    assert request.use_local_history is False
    assert request.restore_history is False
    assert request.playback_history_loader is not None
    assert request.playback_history_saver is not None

    request.playback_history_loader()
    request.playback_history_saver({"position": 45000})

    assert load_calls == ["1$91792$1", "https://pan.quark.cn/s/tg-detail-1"]
    assert save_calls == [("1$91792$1", {"position": 45000})]


def test_build_request_falls_back_to_original_share_link_when_backend_history_key_is_missing() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "1$91792$1",
                "vod_name": "剧集",
                "vod_play_url": "第1集$1@91793@0@0#第2集$1@91794@0@1",
            }
        ]
    }
    load_calls: list[str] = []

    def load_history(vod_id: str):
        load_calls.append(vod_id)
        if vod_id == "https://pan.quark.cn/s/tg-detail-1":
            return {"position": 45000}
        return None

    controller = TelegramSearchController(api, playback_history_loader=load_history)

    request = controller.build_request("https://pan.quark.cn/s/tg-detail-1")

    assert request.playback_history_loader is not None
    assert request.playback_history_loader() == {"position": 45000}
    assert load_calls == ["1$91792$1", "https://pan.quark.cn/s/tg-detail-1"]


def test_build_request_uses_backend_detail_endpoint_for_backend_vod_id() -> None:
    api = FakeApiClient()
    controller = TelegramSearchController(api)

    request = controller.build_request("1$125208$1")

    assert api.detail_calls == []
    assert api.get_detail_calls == ["1$125208$1"]
    assert request.vod.vod_id == "1$125208$1"
    assert request.source_vod_id == "1$125208$1"


def test_local_telegram_search_maps_media_drive_magnet_and_ed2k_results() -> None:
    resources = [
        TelegramResource(
            key="media:-100:1",
            kind="media",
            chat_id=-100,
            msg_id=1,
            chat_title="私密频道",
            title="Movie.2026.2160p.mkv",
            file_name="Movie.2026.2160p.mkv",
            size=2 * 1024 * 1024 * 1024,
            mime_type="video/x-matroska",
        ),
        TelegramResource(
            key="drive:-100:2:0",
            kind="drive",
            chat_id=-100,
            msg_id=2,
            chat_title="私密频道",
            title="夸克分享",
            url="https://pan.quark.cn/s/demo",
        ),
        TelegramResource(
            key="magnet:-100:3:0",
            kind="magnet",
            chat_id=-100,
            msg_id=3,
            chat_title="私密频道",
            title="磁力资源",
            url="magnet:?xt=urn:btih:abcdef",
        ),
        TelegramResource(
            key="ed2k:-100:4:0",
            kind="ed2k",
            chat_id=-100,
            msg_id=4,
            chat_title="私密频道",
            title="电驴资源",
            url="ed2k://|file|demo.mkv|123|ABC|/",
        ),
    ]
    controller = TelegramSearchController(
        FakeApiClient(),
        local_media_service=FakeLocalMediaService(resources),
        prefer_local_media=True,
    )

    categories = controller.load_categories()
    items, _page_count = controller.search_items("资源", page=1)
    channel_items, _channel_page_count = controller.load_items("chat:-100", page=1)

    assert [category.type_id for category in categories] == ["0", "chat:-100"]
    assert [item.vod_id for item in items] == [
        "magnet:?xt=urn:btih:abcdef",
        "ed2k://|file|demo.mkv|123|ABC|/",
    ]
    assert channel_items[0].vod_id == "telegram://media/-100/1"
    assert channel_items[0].vod_remarks == "2.0 GB"


def test_local_telegram_search_and_browse_use_separate_chat_switches() -> None:
    resources = [
        TelegramResource(
            key="media:-100:1",
            kind="media",
            chat_id=-100,
            msg_id=1,
            chat_title="搜索频道",
            title="Search.Movie.mkv",
        ),
        TelegramResource(
            key="drive:-200:2:0",
            kind="drive",
            chat_id=-200,
            msg_id=2,
            chat_title="浏览频道",
            title="Browse Share",
            text="Browse Share",
            url="https://pan.quark.cn/s/browse",
        ),
    ]
    service = FakeLocalMediaService(
        resources,
        chats=[
            TelegramChatInfo(id=-100, title="搜索频道", kind="channel", enabled=True),
            TelegramChatInfo(id=-200, title="浏览频道", kind="channel", enabled=False, browse_enabled=True),
        ],
    )
    controller = TelegramSearchController(FakeApiClient(), local_media_service=service, prefer_local_media=True)

    categories = controller.load_categories()
    search_items, _search_pages = controller.search_items("Movie", page=1)
    browse_items, _browse_pages = controller.load_items("0", page=1)

    assert [category.type_id for category in categories] == ["0", "chat:-200"]
    assert [item.vod_name for item in search_items] == ["Search.Movie.mkv"]
    assert [item.vod_id for item in browse_items] == ["https://pan.quark.cn/s/browse"]


def test_local_telegram_channel_browse_syncs_channel_when_index_is_empty() -> None:
    resource = TelegramResource(
        key="drive:-200:2:0",
        kind="drive",
        chat_id=-200,
        msg_id=2,
        chat_title="浏览频道",
        title="Browse Share",
        text="Browse Share",
        url="https://pan.quark.cn/s/browse",
    )
    service = FakeLocalMediaService(
        [],
        chats=[
            TelegramChatInfo(
                id=-200,
                title="浏览频道",
                kind="channel",
                browse_enabled=True,
            )
        ],
        sync_resources={-200: [resource]},
    )
    controller = TelegramSearchController(
        FakeApiClient(),
        local_media_service=service,
        prefer_local_media=True,
    )

    items, _pages = controller.load_items("chat:-200", page=1)

    assert service.index_page_calls == [(-200, 30, 0)]
    assert [item.vod_id for item in items] == ["https://pan.quark.cn/s/browse"]


def test_local_telegram_channel_browse_schedules_background_index_batches() -> None:
    service = FakeLocalMediaService(
        [],
        chats=[
            TelegramChatInfo(
                id=-200,
                title="浏览频道",
                kind="channel",
                browse_enabled=True,
            )
        ],
        index_page_batches=[
            ([], 30, 90),
            ([], 30, 60),
            ([], 10, 50),
        ],
    )
    controller = TelegramSearchController(
        FakeApiClient(),
        local_media_service=service,
        prefer_local_media=True,
    )

    items, _pages = controller.load_items("chat:-200", page=1)

    for _index in range(100):
        if len(service.index_page_calls) >= 3:
            break
        time.sleep(0.01)

    assert items == []
    assert service.index_page_calls[:3] == [
        (-200, 30, 0),
        (-200, 30, 90),
        (-200, 30, 60),
    ]


def test_local_telegram_channel_search_uses_selected_channel_and_syncs_when_empty() -> None:
    resource = TelegramResource(
        key="media:-200:2",
        kind="media",
        chat_id=-200,
        msg_id=2,
        chat_title="浏览频道",
        title="Browse.Movie.mkv",
    )
    service = FakeLocalMediaService(
        [],
        chats=[
            TelegramChatInfo(
                id=-100,
                title="搜索频道",
                kind="channel",
                enabled=True,
            ),
            TelegramChatInfo(
                id=-200,
                title="浏览频道",
                kind="channel",
                browse_enabled=True,
            ),
        ],
        sync_resources={-200: [resource]},
    )
    controller = TelegramSearchController(
        FakeApiClient(),
        local_media_service=service,
        prefer_local_media=True,
    )

    items, _pages = controller.search_items("Movie", page=1, category_id="chat:-200")

    assert service.index_page_calls == [(-200, 30, 0)]
    assert [item.vod_id for item in items] == ["telegram://media/-200/2"]


def test_telegram_media_service_search_skips_public_channels(tmp_path) -> None:
    repo = TelegramMediaRepository(tmp_path / "app.db")
    repo.upsert_chat(
        TelegramChatInfo(
            id=-100,
            title="私密频道",
            kind="channel",
            username="",
            enabled=True,
        )
    )
    repo.upsert_chat(
        TelegramChatInfo(
            id=-200,
            title="公开频道",
            kind="channel",
            username="public_movies",
            enabled=True,
        )
    )
    repo.upsert_resources(
        [
            TelegramResource(
                key="media:-100:1",
                kind="media",
                chat_id=-100,
                msg_id=1,
                chat_title="私密频道",
                title="Private.Movie.mkv",
            ),
            TelegramResource(
                key="media:-200:2",
                kind="media",
                chat_id=-200,
                msg_id=2,
                chat_title="公开频道",
                title="Public.Movie.mkv",
            ),
        ]
    )
    service = TelegramMediaService(repository=repo)

    results = service.search("Movie")

    assert [resource.title for resource in results] == ["Private.Movie.mkv"]


def test_build_request_for_local_telegram_media_is_direct_playable() -> None:
    resource = TelegramResource(
        key="media:-100:1",
        kind="media",
        chat_id=-100,
        msg_id=1,
        chat_title="私密频道",
        title="Movie.2026.2160p.mkv",
        file_name="Movie.2026.2160p.mkv",
        size=1024,
        mime_type="video/x-matroska",
    )
    controller = TelegramSearchController(
        FakeApiClient(),
        local_media_service=FakeLocalMediaService([resource]),
        prefer_local_media=True,
    )

    request = controller.build_request("telegram://media/-100/1")

    assert request.source_mode == "local_media"
    assert request.playlist[0].url == "telegram://media/-100/1"
    assert request.playlist[0].size == 1024


def test_telegram_media_service_keeps_small_download_offset(tmp_path) -> None:
    payload = bytes(index % 251 for index in range(2 * 1024 * 1024))
    download_calls: list[tuple[int, int]] = []

    class FakeClient:
        async def connect(self) -> None:
            return None

        async def get_messages(self, chat_id: int, ids: int):
            assert chat_id == -100
            assert ids == 1
            return SimpleNamespace(media="media")

        async def iter_download(self, media, *, offset: int = 0, request_size: int = 0):
            assert media == "media"
            download_calls.append((offset, request_size))
            cursor = offset
            while cursor < len(payload):
                next_cursor = min(cursor + request_size, len(payload))
                yield payload[cursor:next_cursor]
                cursor = next_cursor

    service = TelegramMediaService(
        repository=TelegramMediaRepository(tmp_path / "app.db"),
        client_factory=lambda _session, _api_id, _api_hash: FakeClient(),
        request_size=512 * 1024,
    )

    data = b"".join(service.iter_media_bytes(-100, 1, offset=97_319, limit=1000))

    assert data == payload[97_319:98_319]
    assert download_calls == [(97_319, 512 * 1024)]


def test_telegram_media_service_aligns_large_download_offset_and_slices_payload(tmp_path) -> None:
    payload = bytes(index % 251 for index in range(4 * 1024 * 1024))
    download_calls: list[tuple[int, int]] = []

    class FakeClient:
        async def connect(self) -> None:
            return None

        async def get_messages(self, chat_id: int, ids: int):
            assert chat_id == -100
            assert ids == 1
            return SimpleNamespace(media="media")

        async def iter_download(self, media, *, offset: int = 0, request_size: int = 0):
            assert media == "media"
            download_calls.append((offset, request_size))
            cursor = offset
            while cursor < len(payload):
                next_cursor = min(cursor + request_size, len(payload))
                yield payload[cursor:next_cursor]
                cursor = next_cursor

    service = TelegramMediaService(
        repository=TelegramMediaRepository(tmp_path / "app.db"),
        client_factory=lambda _session, _api_id, _api_hash: FakeClient(),
        request_size=512 * 1024,
    )

    data = b"".join(service.iter_media_bytes(-100, 1, offset=1_145_895, limit=1000))

    assert data == payload[1_145_895:1_146_895]
    assert download_calls == [(1_048_576, 512 * 1024)]


def test_telegram_media_repository_extracts_links_and_media_from_indexed_resources(tmp_path) -> None:
    repo = TelegramMediaRepository(tmp_path / "app.db")
    repo.upsert_resources(
        [
            TelegramResource(
                key="media:-100:1",
                kind="media",
                chat_id=-100,
                msg_id=1,
                chat_title="私密频道",
                title="Breaking.Bad.S01E01.mkv",
                file_name="Breaking.Bad.S01E01.mkv",
                size=123,
            ),
            TelegramResource(
                key="drive:-100:2:0",
                kind="drive",
                chat_id=-100,
                msg_id=2,
                chat_title="私密频道",
                title="百度分享",
                text="百度网盘 https://pan.baidu.com/s/demo",
                url="https://pan.baidu.com/s/demo",
            ),
        ]
    )

    assert repo.get_media(-100, 1).file_name == "Breaking.Bad.S01E01.mkv"
    assert [resource.kind for resource in repo.search("Breaking")] == ["media"]
    assert [resource.url for resource in repo.search("百度")] == ["https://pan.baidu.com/s/demo"]


def test_telegram_message_resources_only_index_video_files_as_media() -> None:
    video_by_extension = SimpleNamespace(
        id=1,
        message="",
        file=SimpleNamespace(
            name="Movie.2026.2160p.mkv",
            size=123,
            mime_type="application/octet-stream",
        ),
    )
    video_by_mime_type = SimpleNamespace(
        id=2,
        message="",
        file=SimpleNamespace(name="telegram-video", size=456, mime_type="video/mp4"),
    )
    audio_with_link = SimpleNamespace(
        id=3,
        message="音频资源 https://pan.quark.cn/s/demo",
        file=SimpleNamespace(name="OST.flac", size=789, mime_type="audio/flac"),
    )
    archive = SimpleNamespace(
        id=4,
        message="",
        file=SimpleNamespace(name="Movie.Subtitles.zip", size=10, mime_type="application/zip"),
    )

    assert [
        resource.kind
        for resource in _resources_from_message(video_by_extension, chat_id=-100, chat_title="频道")
    ] == ["media"]
    assert [
        resource.kind
        for resource in _resources_from_message(video_by_mime_type, chat_id=-100, chat_title="频道")
    ] == ["media"]
    assert [
        resource.kind
        for resource in _resources_from_message(audio_with_link, chat_id=-100, chat_title="频道")
    ] == ["drive"]
    assert _resources_from_message(archive, chat_id=-100, chat_title="频道") == []


def test_telegram_media_repository_persists_channel_usage_flags(tmp_path) -> None:
    repo = TelegramMediaRepository(tmp_path / "app.db")
    repo.upsert_chat(TelegramChatInfo(id=-100, title="搜索频道", kind="channel"))
    repo.upsert_chat(TelegramChatInfo(id=-200, title="浏览频道", kind="channel"))
    repo.set_chat_enabled(-100, True)
    repo.set_chat_browse_enabled(-200, True)
    repo.upsert_resources(
        [
            TelegramResource(
                key="media:-100:1",
                kind="media",
                chat_id=-100,
                msg_id=1,
                chat_title="搜索频道",
                title="Search.Movie.mkv",
            ),
            TelegramResource(
                key="media:-200:2",
                kind="media",
                chat_id=-200,
                msg_id=2,
                chat_title="浏览频道",
                title="Browse.Movie.mkv",
            ),
        ]
    )

    chats = {chat.id: chat for chat in repo.list_chats()}

    assert chats[-100].enabled is True
    assert chats[-100].browse_enabled is False
    assert chats[-200].browse_enabled is True
    assert repo.search_enabled_chat_ids() == [-100]
    assert repo.browse_enabled_chat_ids() == [-200]
    assert [resource.chat_id for resource in repo.search("Movie", chat_ids=repo.search_enabled_chat_ids())] == [-100]
