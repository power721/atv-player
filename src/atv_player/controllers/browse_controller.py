from __future__ import annotations

from atv_player.models import OpenPlayerRequest, PlayItem, VodItem


def build_vod_list_path(path: str) -> str:
    normalized = path or "/"
    return f"1${normalized}$1"


def _map_play_item(payload: dict, index: int) -> PlayItem:
    return PlayItem(
        title=str(payload.get("title") or payload.get("name") or ""),
        url=str(payload.get("url") or ""),
        path=str(payload.get("path") or ""),
        index=index,
        size=int(payload.get("size") or 0),
    )


def _map_vod_item(payload: dict) -> VodItem:
    items = [
        _map_play_item(item, index)
        for index, item in enumerate(payload.get("items") or [])
    ]
    return VodItem(
        vod_id=str(payload.get("vod_id") or ""),
        vod_name=str(payload.get("vod_name") or ""),
        path=str(payload.get("path") or ""),
        vod_pic=str(payload.get("vod_pic") or ""),
        vod_tag=str(payload.get("vod_tag") or ""),
        vod_time=str(payload.get("vod_time") or ""),
        vod_remarks=str(payload.get("vod_remarks") or ""),
        vod_play_from=str(payload.get("vod_play_from") or ""),
        vod_play_url=str(payload.get("vod_play_url") or ""),
        type_name=str(payload.get("type_name") or ""),
        vod_content=str(payload.get("vod_content") or ""),
        vod_year=str(payload.get("vod_year") or ""),
        vod_area=str(payload.get("vod_area") or ""),
        vod_lang=str(payload.get("vod_lang") or ""),
        vod_director=str(payload.get("vod_director") or ""),
        vod_actor=str(payload.get("vod_actor") or ""),
        dbid=int(payload.get("dbid") or 0),
        type=int(payload.get("type") or 0),
        items=items,
    )

def filter_search_results(results: list[VodItem], drive_type: str) -> list[VodItem]:
    if not drive_type:
        return list(results)
    return [item for item in results if drive_type in item.type_name]


class BrowseController:
    def __init__(self, api_client) -> None:
        self._api_client = api_client

    def load_folder(self, path: str, page: int = 1, size: int = 50) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_vod(build_vod_list_path(path), page=page, size=size)
        items = [_map_vod_item(item) for item in payload.get("list", [])]
        return items, int(payload.get("total", len(items)))

    def search(self, keyword: str) -> list[VodItem]:
        payload = self._api_client.telegram_search(keyword)
        return [
            VodItem(
                vod_id=str(item.get("id", "")),
                vod_name=str(item.get("name", "")),
                vod_tag="folder",
                vod_time=str(item.get("time", "")),
                type_name=str(item.get("type", "")),
                vod_play_from=str(item.get("channel", "")),
                vod_play_url=str(item.get("link", "")),
            )
            for item in payload
        ]

    def build_playlist_from_folder(
        self,
        folder_items: list[VodItem],
        clicked_vod_id: str,
    ) -> tuple[list[PlayItem], int]:
        playlist: list[PlayItem] = []
        start_index = 0
        for item in folder_items:
            if item.type != 2:
                continue
            index = len(playlist)
            playlist_item = PlayItem(
                title=item.vod_name,
                url=item.vod_play_url,
                path=item.path,
                index=index,
                size=0,
            )
            playlist.append(playlist_item)
            if item.vod_id == clicked_vod_id:
                start_index = index
        return playlist, start_index

    def resolve_search_result(self, item: VodItem) -> str:
        return self._api_client.resolve_share_link(item.vod_play_url)

    def build_request_from_detail(self, vod_id: str) -> OpenPlayerRequest:
        payload = self._api_client.get_detail(vod_id)
        detail = _map_vod_item(payload["list"][0])
        return OpenPlayerRequest(
            vod=detail,
            playlist=detail.items,
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )

    def build_request_from_folder_item(
        self,
        clicked_item: VodItem,
        folder_items: list[VodItem],
    ) -> OpenPlayerRequest:
        playlist, clicked_index = self.build_playlist_from_folder(folder_items, clicked_item.vod_id)
        vod = VodItem(
            vod_id=clicked_item.vod_id,
            vod_name=clicked_item.vod_name,
            vod_pic=clicked_item.vod_pic,
            path=clicked_item.path,
            vod_remarks=clicked_item.vod_remarks,
            type_name=clicked_item.type_name,
            vod_content=clicked_item.vod_content,
            vod_year=clicked_item.vod_year,
            vod_area=clicked_item.vod_area,
            vod_lang=clicked_item.vod_lang,
            vod_director=clicked_item.vod_director,
            vod_actor=clicked_item.vod_actor,
            dbid=clicked_item.dbid,
            type=clicked_item.type,
        )
        return OpenPlayerRequest(
            vod=vod,
            playlist=playlist,
            clicked_index=clicked_index,
            source_mode="folder",
            source_path=clicked_item.path.rsplit("/", 1)[0] or "/",
            source_vod_id=clicked_item.vod_id,
            source_clicked_vod_id=clicked_item.vod_id,
        )
