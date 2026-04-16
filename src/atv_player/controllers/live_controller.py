from __future__ import annotations

from atv_player.controllers.browse_controller import _map_vod_item
from atv_player.controllers.douban_controller import _map_category, _map_item
from atv_player.models import DoubanCategory, OpenPlayerRequest, PlayItem, VodItem


def _looks_like_stream_url(value: str) -> bool:
    candidate = value.strip().lower()
    return candidate.startswith(
        (
            "http://",
            "https://",
            "rtmp://",
            "rtmps://",
            "rtsp://",
            "udp://",
            "mms://",
        )
    ) or any(ext in candidate for ext in (".m3u8", ".flv", ".mp4"))


def _parse_live_playlist(vod_play_from: str, vod_play_url: str) -> list[PlayItem]:
    playlist: list[PlayItem] = []
    route_names = [name.strip() for name in (vod_play_from or "").split("$$$")]
    route_groups = (vod_play_url or "").split("$$$")
    for group_index, raw_group in enumerate(route_groups):
        route_name = route_names[group_index] if group_index < len(route_names) else ""
        for raw_chunk in raw_group.split("#"):
            chunk = raw_chunk.strip()
            if not chunk:
                continue
            title, separator, value = chunk.partition("$")
            if not separator:
                title = chunk
                value = chunk
            title = title.strip() or value.strip() or f"线路 {len(playlist) + 1}"
            if route_name:
                title = f"{route_name} | {title}"
            value = value.strip()
            playlist.append(
                PlayItem(
                    title=title,
                    url=value if _looks_like_stream_url(value) else "",
                    vod_id="" if _looks_like_stream_url(value) else value,
                    index=len(playlist),
                )
            )
    return playlist


class LiveController:
    _PAGE_SIZE = 30

    def __init__(self, api_client) -> None:
        self._api_client = api_client

    def load_categories(self) -> list[DoubanCategory]:
        payload = self._api_client.list_live_categories()
        categories = [_map_category(item) for item in payload.get("class", [])]
        categories = [category for category in categories if category.type_id != "0"]
        return [DoubanCategory(type_id="0", type_name="推荐"), *categories]

    def _map_live_items(self, payload: dict) -> list[VodItem]:
        return [_map_item(item) for item in payload.get("list", [])]

    def load_items(self, category_id: str, page: int) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_live_items(category_id, page=page)
        items = self._map_live_items(payload)
        total_raw = payload.get("total")
        if total_raw is not None:
            total = int(total_raw)
        else:
            pagecount = int(payload.get("pagecount") or 0)
            total = pagecount * self._PAGE_SIZE
        return items, total

    def load_folder_items(self, vod_id: str) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_live_items(vod_id, page=1)
        items = self._map_live_items(payload)
        total_raw = payload.get("total")
        total = int(total_raw) if total_raw is not None else len(items)
        return items, total

    def _build_playlist(self, detail: VodItem) -> list[PlayItem]:
        detail_items = [item for item in detail.items if item.url.strip()]
        if detail_items:
            return [
                PlayItem(
                    title=item.title or f"线路 {index + 1}",
                    url=item.url,
                    vod_id=item.vod_id,
                    index=index,
                    headers=dict(item.headers),
                )
                for index, item in enumerate(detail_items)
            ]
        return [item for item in _parse_live_playlist(detail.vod_play_from, detail.vod_play_url) if item.url]

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        payload = self._api_client.get_live_detail(vod_id)
        detail = _map_vod_item(payload["list"][0])
        playlist = self._build_playlist(detail)
        if not playlist:
            raise ValueError(f"没有可播放的项目: {detail.vod_name}")
        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            clicked_index=0,
            source_mode="detail",
            source_vod_id=detail.vod_id,
        )
