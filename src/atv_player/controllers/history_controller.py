from atv_player.models import HistoryRecord


class HistoryController:
    def __init__(self, api_client) -> None:
        self._api_client = api_client

    def load_page(self, page: int, size: int) -> tuple[list[HistoryRecord], int]:
        payload = self._api_client.list_history(page, size)
        records = [
            HistoryRecord(
                id=item["id"],
                key=item["key"],
                vod_name=item["vodName"],
                vod_pic=item.get("vodPic", ""),
                vod_remarks=item.get("vodRemarks", ""),
                episode=item.get("episode", 0),
                episode_url=item.get("episodeUrl", ""),
                position=item.get("position", 0),
                opening=item.get("opening", 0),
                ending=item.get("ending", 0),
                speed=item.get("speed", 1.0),
                create_time=item["createTime"],
            )
            for item in payload["content"]
        ]
        return records, int(payload["totalElements"])

    def delete_one(self, history_id: int) -> None:
        self._api_client.delete_history(history_id)

    def delete_many(self, history_ids: list[int]) -> None:
        self._api_client.delete_histories(history_ids)

    def clear_all(self) -> None:
        self._api_client.clear_history()
