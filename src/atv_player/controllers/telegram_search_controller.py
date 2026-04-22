from __future__ import annotations

from atv_player.controllers.browse_controller import _map_vod_item
from atv_player.controllers.douban_controller import _map_category, _map_item
from atv_player.models import DoubanCategory, OpenPlayerRequest, PlayItem, VodItem


def _looks_like_media_url(value: str) -> bool:
    candidate = value.strip().lower()
    return candidate.startswith(("http://", "https://", "rtmp://", "rtsp://")) or any(
        ext in candidate for ext in (".m3u8", ".mp4", ".flv")
    )


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
    playlist = _parse_playlist(detail.vod_play_url)
    if not playlist and detail.items:
        playlist = list(detail.items)
    return playlist


class TelegramSearchController:
    _PAGE_SIZE = 30

    def __init__(self, api_client) -> None:
        self._api_client = api_client

    def load_categories(self) -> list[DoubanCategory]:
        payload = self._api_client.list_telegram_search_categories()
        categories = [_map_category(item) for item in payload.get("class", [])]
        categories = [category for category in categories if category.type_id != "0"]
        return [DoubanCategory(type_id="0", type_name="推荐"), *categories]

    def load_items(self, category_id: str, page: int) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_telegram_search_items(category_id, page=page)
        items = [_map_item(item) for item in payload.get("list", [])]
        total_raw = payload.get("total")
        if total_raw is not None:
            total = int(total_raw)
        else:
            pagecount = int(payload.get("pagecount") or 0)
            total = pagecount * self._PAGE_SIZE
        return items, total

    def search_items(self, keyword: str, page: int) -> tuple[list[VodItem], int]:
        payload = self._api_client.search_telegram_items(keyword, page=page)
        items = [_map_item(item) for item in payload.get("list", [])]
        total_raw = payload.get("total")
        if total_raw is not None:
            total = int(total_raw)
        else:
            pagecount = int(payload.get("pagecount") or 0)
            total = pagecount * self._PAGE_SIZE
        return items, total

    def resolve_playlist_item(self, item: PlayItem) -> VodItem | None:
        if not item.vod_id:
            return None
        try:
            payload = self._api_client.get_detail(item.vod_id)
            return _map_vod_item(payload["list"][0])
        except (KeyError, IndexError):
            return None

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        payload = self._api_client.get_telegram_search_detail(vod_id)
        detail = _map_vod_item(payload["list"][0])
        playlist = build_detail_playlist(detail)
        if not playlist:
            raise ValueError(f"没有可播放的项目: {detail.vod_name}")
        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            clicked_index=0,
            source_kind="browse",
            source_mode="detail",
            source_vod_id=detail.vod_id,
            detail_resolver=self.resolve_playlist_item,
        )
