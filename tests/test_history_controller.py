from atv_player.controllers.history_controller import HistoryController
from atv_player.models import HistoryRecord


class FakeApiClient:
    def __init__(self) -> None:
        self.deleted_one: list[int] = []
        self.deleted_many: list[list[int]] = []
        self.cleared = False

    def list_history(self, page: int, size: int) -> dict:
        return {
            "content": [
                {
                    "id": 9,
                    "key": "movie-1",
                    "vodName": "Movie",
                    "vodPic": "pic",
                    "vodRemarks": "Episode 2",
                    "episode": 1,
                    "episodeUrl": "2.m3u8",
                    "position": 90000,
                    "opening": 0,
                    "ending": 0,
                    "speed": 1.0,
                    "createTime": 123456,
                }
            ],
            "totalElements": 1,
        }

    def delete_history(self, history_id: int) -> None:
        self.deleted_one.append(history_id)

    def delete_histories(self, history_ids: list[int]) -> None:
        self.deleted_many.append(history_ids)

    def clear_history(self) -> None:
        self.cleared = True


class FakeRepository:
    def __init__(self, histories: list[HistoryRecord] | None = None) -> None:
        self.histories = list(histories or [])
        self.deleted: list[tuple[int, str]] = []

    def list_playback_histories(self) -> list[HistoryRecord]:
        return list(self.histories)

    def delete_playback_history(self, plugin_id: int, vod_id: str) -> None:
        self.deleted.append((plugin_id, vod_id))


def test_history_controller_maps_backend_payload() -> None:
    controller = HistoryController(FakeApiClient())

    records, total = controller.load_page(page=1, size=20)

    assert total == 1
    assert records[0].id == 9
    assert records[0].vod_name == "Movie"
    assert records[0].episode == 1
    assert records[0].source_kind == "remote"
    assert records[0].source_plugin_id == 0
    assert records[0].source_plugin_name == ""


def test_history_controller_deletes_one_or_many() -> None:
    api = FakeApiClient()
    controller = HistoryController(api)
    record_one = HistoryRecord(
        id=9,
        key="movie-1",
        vod_name="Movie",
        vod_pic="pic",
        vod_remarks="Episode 2",
        episode=1,
        episode_url="2.m3u8",
        position=90000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=123456,
        source_kind="remote",
    )
    record_two = HistoryRecord(
        id=10,
        key="movie-2",
        vod_name="Movie 2",
        vod_pic="pic-2",
        vod_remarks="Episode 1",
        episode=0,
        episode_url="1.m3u8",
        position=3000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=123457,
        source_kind="remote",
    )

    controller.delete_one(record_one)
    controller.delete_many([record_one, record_two])
    controller.clear_page([record_one, record_two])

    assert api.deleted_one == [9]
    assert api.deleted_many == [[9, 10], [9, 10]]


def test_history_controller_tolerates_missing_optional_fields() -> None:
    class MissingFieldApiClient(FakeApiClient):
        def list_history(self, page: int, size: int) -> dict:
            return {
                "content": [
                    {
                        "id": 10,
                        "key": "movie-2",
                        "vodName": "Movie 2",
                        "vodRemarks": "Episode 1",
                        "episode": 0,
                        "position": 3000,
                        "createTime": 999,
                    }
                ],
                "totalElements": 1,
            }

    controller = HistoryController(MissingFieldApiClient())

    records, total = controller.load_page(page=1, size=20)

    assert total == 1
    assert records[0].id == 10
    assert records[0].vod_pic == ""
    assert records[0].episode_url == ""
    assert records[0].speed == 1.0


def test_history_controller_merges_remote_and_plugin_histories_in_descending_time_order() -> None:
    api = FakeApiClient()
    repository = FakeRepository(
        histories=[
            HistoryRecord(
                id=0,
                key="plugin-1",
                vod_name="Plugin Movie",
                vod_pic="plugin-pic",
                vod_remarks="第2集",
                episode=1,
                episode_url="plugin-2.m3u8",
                position=45000,
                opening=0,
                ending=0,
                speed=1.0,
                create_time=200000,
                source_kind="spider_plugin",
                source_plugin_id=7,
                source_plugin_name="红果短剧",
            )
        ]
    )
    controller = HistoryController(api, repository)

    records, total = controller.load_page(page=1, size=20)

    assert total == 2
    assert [record.key for record in records] == ["plugin-1", "movie-1"]
    assert [record.source_kind for record in records] == ["spider_plugin", "remote"]


def test_history_controller_deletes_one_or_many_by_source() -> None:
    api = FakeApiClient()
    repository = FakeRepository()
    controller = HistoryController(api, repository)
    remote = HistoryRecord(
        id=9,
        key="movie-1",
        vod_name="Movie",
        vod_pic="pic",
        vod_remarks="Episode 2",
        episode=1,
        episode_url="2.m3u8",
        position=90000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=123456,
        source_kind="remote",
    )
    plugin = HistoryRecord(
        id=0,
        key="detail-1",
        vod_name="Plugin Movie",
        vod_pic="poster",
        vod_remarks="第1集",
        episode=0,
        episode_url="1.m3u8",
        position=15000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=123457,
        source_kind="spider_plugin",
        source_plugin_id=3,
        source_plugin_name="红果短剧",
    )

    controller.delete_one(remote)
    controller.delete_many([remote, plugin])

    assert api.deleted_one == [9]
    assert api.deleted_many == [[9]]
    assert repository.deleted == [(3, "detail-1")]


def test_history_controller_clear_page_deletes_current_records_by_source() -> None:
    api = FakeApiClient()
    repository = FakeRepository()
    controller = HistoryController(api, repository)
    remote = HistoryRecord(
        id=11,
        key="movie-2",
        vod_name="Movie 2",
        vod_pic="",
        vod_remarks="Episode 1",
        episode=0,
        episode_url="1.m3u8",
        position=3000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=999,
        source_kind="remote",
    )
    plugin = HistoryRecord(
        id=0,
        key="detail-2",
        vod_name="Plugin Movie",
        vod_pic="",
        vod_remarks="第3集",
        episode=2,
        episode_url="3.m3u8",
        position=6000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1000,
        source_kind="spider_plugin",
        source_plugin_id=4,
        source_plugin_name="插件二",
    )

    controller.clear_page([remote, plugin])

    assert api.deleted_many == [[11]]
    assert repository.deleted == [(4, "detail-2")]
