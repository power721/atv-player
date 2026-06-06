from __future__ import annotations

from collections.abc import Callable
import threading

from atv_player.controllers.browse_controller import _map_vod_item
from atv_player.controllers.douban_controller import _map_category, _map_item
from atv_player.controllers.pagination import page_count_from_payload
from atv_player.models import DoubanCategory, HistoryRecord, OpenPlayerRequest, PlayItem, VodItem
from atv_player.telegram_media import (
    TelegramMediaService,
    TelegramResource,
    parse_telegram_media_uri,
    telegram_media_uri,
)

_TELEGRAM_CHAT_CATEGORY_PREFIX = "chat:"
_LOCAL_CHAT_INITIAL_INDEX_LIMIT = 30
_LOCAL_CHAT_BACKGROUND_BATCH_LIMIT = 30
_LOCAL_CHAT_BACKGROUND_BATCHES = 4


def _looks_like_media_url(value: str) -> bool:
    candidate = value.strip().lower()
    return candidate.startswith(("http://", "https://", "rtmp://", "rtsp://", "telegram://media/")) or any(
        candidate.endswith(ext) or f"{ext}?" in candidate for ext in (".m3u8", ".mkv", ".mp4", ".flv")
    )


def _looks_like_backend_vod_id(value: str) -> bool:
    candidate = value.strip()
    return "$" in candidate and not candidate.startswith(("http://", "https://"))


def _parse_playlist(vod_play_url: str) -> list[PlayItem]:
    playlist: list[PlayItem] = []
    for chunk in (vod_play_url or "").split("#"):
        if not chunk:
            continue
        title, separator, value = chunk.partition("$")
        if not separator:
            clean_value = title.strip()
            if not _looks_like_media_url(clean_value):
                clean_value = ""
        else:
            clean_value = value.strip()
        if not clean_value:
            continue
        playlist.append(
            PlayItem(
                title=title.strip(),
                url=clean_value if _looks_like_media_url(clean_value) else "",
                index=len(playlist),
                vod_id="" if _looks_like_media_url(clean_value) else clean_value,
            )
        )
    return playlist


def build_detail_playlist(detail: VodItem) -> list[PlayItem]:
    if detail.items and len(detail.items) == 1 and detail.items[0].url and _looks_like_media_url(detail.vod_play_url):
        return list(detail.items)
    playlist = _parse_playlist(detail.vod_play_url)
    if not playlist and detail.items:
        playlist = list(detail.items)
    return playlist


def _format_size(size: int) -> str:
    if size <= 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return ""


def _resource_type_name(kind: str) -> str:
    return {
        "media": "Telegram媒体",
        "drive": "网盘链接",
        "magnet": "磁力链接",
        "ed2k": "电驴链接",
    }.get(kind, kind)


def _map_local_resource(resource: TelegramResource) -> VodItem:
    if resource.kind == "media":
        vod_id = telegram_media_uri(resource.chat_id, resource.msg_id)
        remarks = _format_size(resource.size) or "可在线播放"
    else:
        vod_id = resource.url
        remarks = {"drive": "网盘", "magnet": "磁力", "ed2k": "电驴"}.get(resource.kind, resource.kind)
    return VodItem(
        vod_id=vod_id,
        vod_name=resource.title or resource.file_name or resource.url,
        vod_remarks=remarks,
        vod_play_from=resource.chat_title,
        vod_play_url=vod_id,
        type_name=_resource_type_name(resource.kind),
        vod_content=resource.text,
    )


def _chat_category_id(chat_id: int | str) -> str:
    return f"{_TELEGRAM_CHAT_CATEGORY_PREFIX}{int(chat_id)}"


def _parse_chat_category_id(category_id: str) -> int | None:
    value = str(category_id or "").strip()
    if not value.startswith(_TELEGRAM_CHAT_CATEGORY_PREFIX):
        return None
    try:
        return int(value.removeprefix(_TELEGRAM_CHAT_CATEGORY_PREFIX))
    except ValueError:
        return None


class TelegramSearchController:
    _PAGE_SIZE = 30
    uses_page_count_for_pagination = True

    def __init__(
        self,
        api_client,
        playback_history_loader: Callable[[str], HistoryRecord | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
        local_media_service: TelegramMediaService | None = None,
        prefer_local_media: bool = False,
    ) -> None:
        self._api_client = api_client
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver
        self._local_media_service = local_media_service
        self._prefer_local_media = prefer_local_media
        self._local_index_lock = threading.Lock()
        self._local_background_indexing_chat_ids: set[int] = set()

    def load_categories(self) -> list[DoubanCategory]:
        if self._use_local_media():
            chats = self._local_media_service.list_chats() if self._local_media_service is not None else []
            browse_chats = [chat for chat in chats if getattr(chat, "browse_enabled", False)]
            return [
                DoubanCategory(type_id="0", type_name="全部"),
                *[
                    DoubanCategory(type_id=_chat_category_id(chat.id), type_name=chat.title or str(chat.id))
                    for chat in browse_chats
                ],
            ]
        payload = self._api_client.list_telegram_search_categories()
        categories = [_map_category(item) for item in payload.get("class", [])]
        categories = [category for category in categories if category.type_id != "0"]
        return [DoubanCategory(type_id="0", type_name="推荐"), *categories]

    def load_items(
        self,
        category_id: str,
        page: int,
        filters: dict[str, str] | None = None,
    ) -> tuple[list[VodItem], int]:
        if self._use_local_media():
            offset = max(0, page - 1) * self._PAGE_SIZE
            if self._local_media_service is None:
                resources = []
            elif (chat_id := _parse_chat_category_id(category_id)) is not None:
                resources = self._local_media_service.browse_chat(chat_id, limit=self._PAGE_SIZE, offset=offset)
                if not resources and offset == 0:
                    self._index_local_chat_for_first_page(chat_id)
                    resources = self._local_media_service.browse_chat(chat_id, limit=self._PAGE_SIZE, offset=offset)
            else:
                resources = self._local_media_service.browse_recent(limit=self._PAGE_SIZE, offset=offset)
            return [_map_local_resource(resource) for resource in resources], page
        payload = self._api_client.list_telegram_search_items(category_id, page=page)
        items = [_map_item(item) for item in payload.get("list", [])]
        page_count = page_count_from_payload(payload, fallback_total=len(items), page_size=self._PAGE_SIZE)
        return items, page_count

    def search_items(self, keyword: str, page: int, category_id: str = "") -> tuple[list[VodItem], int]:
        if self._use_local_media():
            offset = max(0, page - 1) * self._PAGE_SIZE
            if (chat_id := _parse_chat_category_id(category_id)) is not None:
                resources = self._local_media_service.search_chat(  # type: ignore[union-attr]
                    chat_id,
                    keyword,
                    limit=self._PAGE_SIZE,
                    offset=offset,
                )
                if not resources and offset == 0:
                    self._index_local_chat_for_first_page(chat_id)
                    resources = self._local_media_service.search_chat(  # type: ignore[union-attr]
                        chat_id,
                        keyword,
                        limit=self._PAGE_SIZE,
                        offset=offset,
                    )
            else:
                resources = self._local_media_service.search(  # type: ignore[union-attr]
                    keyword,
                    limit=self._PAGE_SIZE,
                    offset=offset,
                )
            return [_map_local_resource(resource) for resource in resources], page
        payload = self._api_client.search_telegram_items(keyword, page=page)
        items = [_map_item(item) for item in payload.get("list", [])]
        page_count = page_count_from_payload(payload, fallback_total=len(items), page_size=self._PAGE_SIZE)
        return items, page_count

    def _index_local_chat_for_first_page(self, chat_id: int) -> None:
        batch = self._index_local_chat_page(
            chat_id,
            limit=_LOCAL_CHAT_INITIAL_INDEX_LIMIT,
            offset_id=0,
        )
        oldest_msg_id = int(getattr(batch, "oldest_msg_id", 0) or 0)
        message_count = int(getattr(batch, "message_count", 0) or 0)
        if oldest_msg_id > 0 and message_count >= _LOCAL_CHAT_INITIAL_INDEX_LIMIT:
            self._start_local_chat_background_index(chat_id, oldest_msg_id)

    def _index_local_chat_page(self, chat_id: int, *, limit: int, offset_id: int = 0):
        if self._local_media_service is None:
            return None
        pager = getattr(self._local_media_service, "index_chat_recent_page", None)
        if callable(pager):
            return pager(chat_id, limit=limit, offset_id=offset_id)
        if offset_id > 0:
            return None
        indexer = getattr(self._local_media_service, "index_chat_recent", None)
        if callable(indexer):
            return indexer(chat_id, limit=limit)
        syncer = getattr(self._local_media_service, "sync_chat", None)
        if callable(syncer):
            return syncer(chat_id, limit=limit)
        return None

    def _start_local_chat_background_index(self, chat_id: int, offset_id: int) -> None:
        with self._local_index_lock:
            if chat_id in self._local_background_indexing_chat_ids:
                return
            self._local_background_indexing_chat_ids.add(chat_id)
        threading.Thread(
            target=self._index_local_chat_background_worker,
            args=(chat_id, offset_id),
            daemon=True,
        ).start()

    def _index_local_chat_background_worker(self, chat_id: int, offset_id: int) -> None:
        try:
            current_offset_id = int(offset_id)
            for _index in range(_LOCAL_CHAT_BACKGROUND_BATCHES):
                if current_offset_id <= 0:
                    break
                batch = self._index_local_chat_page(
                    chat_id,
                    limit=_LOCAL_CHAT_BACKGROUND_BATCH_LIMIT,
                    offset_id=current_offset_id,
                )
                message_count = int(getattr(batch, "message_count", 0) or 0)
                next_offset_id = int(getattr(batch, "oldest_msg_id", 0) or 0)
                if message_count <= 0 or next_offset_id <= 0 or next_offset_id == current_offset_id:
                    break
                current_offset_id = next_offset_id
                if message_count < _LOCAL_CHAT_BACKGROUND_BATCH_LIMIT:
                    break
        finally:
            with self._local_index_lock:
                self._local_background_indexing_chat_ids.discard(chat_id)

    def _use_local_media(self) -> bool:
        return self._local_media_service is not None and self._prefer_local_media

    def refresh_local_chats(self):
        if self._local_media_service is None:
            return []
        return self._local_media_service.refresh_chats()

    def list_local_chats(self):
        if self._local_media_service is None:
            return []
        return self._local_media_service.list_chats()

    def set_local_chat_enabled(self, chat_id: int, enabled: bool) -> None:
        if self._local_media_service is not None:
            self._local_media_service.set_chat_enabled(chat_id, enabled)

    def set_local_chat_browse_enabled(self, chat_id: int, enabled: bool) -> None:
        if self._local_media_service is not None:
            self._local_media_service.set_chat_browse_enabled(chat_id, enabled)

    def sync_local_chats(self, *, limit_per_chat: int | None = 500) -> int:
        if self._local_media_service is None:
            return 0
        return self._local_media_service.sync_enabled_chats(limit_per_chat=limit_per_chat)

    def get_local_user_info(self):
        if self._local_media_service is None:
            return None
        return self._local_media_service.get_user_info()

    def send_local_login_code(self, phone: str):
        if self._local_media_service is None:
            raise ValueError("未配置 Telegram 登录")
        return self._local_media_service.send_login_code(phone)

    def sign_in_local(self, *, phone: str, code: str, password: str = ""):
        if self._local_media_service is None:
            raise ValueError("未配置 Telegram 登录")
        return self._local_media_service.sign_in(phone=phone, code=code, password=password)

    def start_local_qr_login(self):
        if self._local_media_service is None:
            raise ValueError("未配置 Telegram 登录")
        return self._local_media_service.start_qr_login()

    def complete_local_qr_login(self, *, password: str = ""):
        if self._local_media_service is None:
            raise ValueError("未配置 Telegram 登录")
        return self._local_media_service.complete_qr_login(password=password)

    def logout_local(self) -> None:
        if self._local_media_service is not None:
            self._local_media_service.logout()

    def resolve_playlist_item(self, item: PlayItem) -> VodItem | None:
        if not item.vod_id:
            return None
        try:
            payload = self._api_client.get_detail(item.vod_id)
            return _map_vod_item(payload["list"][0])
        except (KeyError, IndexError):
            return None

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        parsed_media = parse_telegram_media_uri(vod_id)
        if parsed_media is not None:
            chat_id, msg_id = parsed_media
            resource = self._local_media_service.get_media(chat_id, msg_id) if self._local_media_service is not None else None
            title = resource.title if resource is not None else f"Telegram {chat_id}/{msg_id}"
            size = resource.size if resource is not None else 0
            mime_type = resource.mime_type if resource is not None else ""
            detail = VodItem(
                vod_id=vod_id,
                vod_name=title,
                vod_play_url=vod_id,
                vod_play_from="Telegram",
                vod_remarks=_format_size(size),
                type_name="Telegram媒体",
                items=[
                    PlayItem(
                        title=title,
                        url=vod_id,
                        vod_id=vod_id,
                        size=size,
                        index=0,
                        media_title=title,
                    )
                ],
            )
            if mime_type:
                detail.vod_content = mime_type
            return OpenPlayerRequest(
                vod=detail,
                playlist=detail.items,
                clicked_index=0,
                source_kind="telegram",
                source_mode="local_media",
                source_vod_id=vod_id,
                use_local_history=False,
            )
        if str(vod_id or "").lower().startswith(("magnet:", "ed2k://")):
            raise ValueError("离线下载链接不能直接在线播放")
        if _looks_like_backend_vod_id(vod_id):
            payload = self._api_client.get_detail(vod_id)
        else:
            payload = self._api_client.get_telegram_search_detail(vod_id)
        detail = _map_vod_item(payload["list"][0])
        playlist = build_detail_playlist(detail)
        if not playlist:
            raise ValueError(f"没有可播放的项目: {detail.vod_name}")
        media_title = str(detail.vod_name or "").strip()
        if media_title:
            for item in playlist:
                if not item.media_title:
                    item.media_title = media_title
        history_loader = None
        history_saver = None
        source_vod_id = str(detail.vod_id or vod_id or "").strip()
        legacy_history_vod_id = str(vod_id or "").strip()
        if self._playback_history_loader is not None:
            def history_loader(source_vod_id=source_vod_id, legacy_history_vod_id=legacy_history_vod_id):
                history = self._playback_history_loader(source_vod_id)
                if history is None and legacy_history_vod_id and legacy_history_vod_id != source_vod_id:
                    history = self._playback_history_loader(legacy_history_vod_id)
                return history
        if self._playback_history_saver is not None:
            history_saver = lambda payload, source_vod_id=source_vod_id: self._playback_history_saver(source_vod_id, payload)
        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            clicked_index=0,
            source_kind="telegram",
            source_mode="detail",
            source_vod_id=source_vod_id,
            detail_resolver=self.resolve_playlist_item,
            use_local_history=False,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )
