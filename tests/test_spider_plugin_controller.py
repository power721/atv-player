from atv_player.plugins.controller import SpiderPluginController


class FakeSpider:
    def homeContent(self, filter):
        return {
            "class": [
                {"type_id": "hot", "type_name": "热门"},
                {"type_id": "tv", "type_name": "剧场"},
            ],
            "list": [
                {"vod_id": "/detail/home-1", "vod_name": "首页推荐", "vod_pic": "poster-home"},
            ],
        }

    def categoryContent(self, tid, pg, filter, extend):
        return {
            "list": [
                {"vod_id": f"/detail/{tid}-{pg}", "vod_name": f"{tid}-{pg}", "vod_pic": "poster-cat", "vod_remarks": "更新中"},
            ],
            "page": pg,
            "pagecount": 3,
            "total": 90,
        }

    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "红果短剧",
                    "vod_pic": "poster-detail",
                    "vod_play_from": "备用线$$$极速线",
                    "vod_play_url": "第1集$/play/1#第2集$https://media.example/2.m3u8$$$第3集$/play/3",
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        return {"parse": 0, "url": f"https://stream.example{id}.m3u8", "header": {"Referer": "https://site.example"}}

    def searchContent(self, key, quick, pg="1"):
        return {
            "list": [{"vod_id": f"/detail/{key}", "vod_name": key, "vod_pic": "poster-search"}],
            "total": 1,
        }


def test_controller_load_categories_prepends_home_when_home_list_exists() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    categories = controller.load_categories()
    items, total = controller.load_items("home", 1)

    assert [item.type_name for item in categories] == ["推荐", "热门", "剧场"]
    assert [item.vod_name for item in items] == ["首页推荐"]
    assert total == 1


def test_controller_search_and_category_mapping() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    items, total = controller.search_items("庆余年", 1)
    category_items, category_total = controller.load_items("tv", 2)

    assert total == 1
    assert items[0].vod_name == "庆余年"
    assert category_total == 90
    assert category_items[0].vod_name == "tv-2"


def test_controller_build_request_defers_player_content_until_episode_load() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    request = controller.build_request("/detail/1")
    first = request.playlist[0]
    second = request.playlist[1]

    assert first.title == "备用线 | 第1集"
    assert first.url == ""
    assert first.play_source == "备用线"
    assert first.vod_id == "/play/1"
    assert second.url == "https://media.example/2.m3u8"

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.headers == {"Referer": "https://site.example"}
