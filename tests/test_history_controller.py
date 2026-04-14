from atv_player.controllers.history_controller import HistoryController


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


def test_history_controller_maps_backend_payload() -> None:
    controller = HistoryController(FakeApiClient())

    records, total = controller.load_page(page=1, size=20)

    assert total == 1
    assert records[0].id == 9
    assert records[0].vod_name == "Movie"
    assert records[0].episode == 1


def test_history_controller_deletes_one_or_many() -> None:
    api = FakeApiClient()
    controller = HistoryController(api)

    controller.delete_one(9)
    controller.delete_many([9, 10])
    controller.clear_all()

    assert api.deleted_one == [9]
    assert api.deleted_many == [[9, 10]]
    assert api.cleared is True


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
