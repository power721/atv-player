from atv_player.models import PlayItem, VodItem


def filter_search_results(results: list[VodItem], drive_type: str) -> list[VodItem]:
    if not drive_type:
        return list(results)
    return [item for item in results if drive_type in item.type_name]


class BrowseController:
    def __init__(self, api_client) -> None:
        self._api_client = api_client

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
