from __future__ import annotations

from atv_player.models import DoubanCategory, VodItem


def _map_category(payload: dict) -> DoubanCategory:
    return DoubanCategory(
        type_id=str(payload.get("type_id") or ""),
        type_name=str(payload.get("type_name") or ""),
    )


def _map_item(payload: dict) -> VodItem:
    return VodItem(
        vod_id=str(payload.get("vod_id") or ""),
        vod_name=str(payload.get("vod_name") or ""),
        vod_pic=str(payload.get("vod_pic") or ""),
        vod_remarks=str(payload.get("vod_remarks") or ""),
        dbid=int(payload.get("dbid") or 0),
        type_name=str(payload.get("type_name") or ""),
        vod_content=str(payload.get("vod_content") or ""),
    )


class DoubanController:
    _PAGE_SIZE = 30

    def __init__(self, api_client) -> None:
        self._api_client = api_client

    def load_categories(self) -> list[DoubanCategory]:
        payload = self._api_client.list_douban_categories()
        return [_map_category(item) for item in payload.get("class", [])]

    def load_items(self, category_id: str, page: int) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_douban_items(category_id, page=page, size=self._PAGE_SIZE)
        items = [_map_item(item) for item in payload.get("list", [])]
        total_raw = payload.get("total")
        if total_raw is not None:
            total = int(total_raw)
        else:
            pagecount = int(payload.get("pagecount") or 0)
            total = pagecount * self._PAGE_SIZE
        return items, total
