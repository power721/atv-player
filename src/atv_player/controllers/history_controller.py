from atv_player.models import HistoryRecord


class HistoryController:
    def __init__(self, api_client, plugin_repository=None) -> None:
        self._api_client = api_client
        self._plugin_repository = plugin_repository

    def _load_remote_records(self) -> list[HistoryRecord]:
        payload = self._api_client.list_history(page=1, size=10000)
        return [
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
                source_kind="remote",
            )
            for item in payload["content"]
        ]

    def load_page(self, page: int, size: int) -> tuple[list[HistoryRecord], int]:
        records = self._load_remote_records()
        if self._plugin_repository is not None:
            records.extend(self._plugin_repository.list_playback_histories())
        records.sort(key=lambda item: item.create_time, reverse=True)
        total = len(records)
        start = max(page - 1, 0) * size
        end = start + size
        return records[start:end], total

    def delete_one(self, record: HistoryRecord) -> None:
        if record.source_kind == "remote":
            self._api_client.delete_history(record.id)
            return
        if self._plugin_repository is None:
            return
        self._plugin_repository.delete_playback_history(record.source_plugin_id, record.key)

    def delete_many(self, records: list[HistoryRecord]) -> None:
        remote_ids = [record.id for record in records if record.source_kind == "remote"]
        if remote_ids:
            self._api_client.delete_histories(remote_ids)
        if self._plugin_repository is None:
            return
        for record in records:
            if record.source_kind == "spider_plugin":
                self._plugin_repository.delete_playback_history(record.source_plugin_id, record.key)

    def clear_page(self, records: list[HistoryRecord]) -> None:
        self.delete_many(records)
