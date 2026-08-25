import logging
import json

import httpx
import pytest

from atv_player.api import ApiClient, ApiError, UnauthorizedError
from atv_player.models import HistoryRecord
from atv_player.network_proxy import ProxyConfig, ProxyDecider


class RaisingTransport(httpx.BaseTransport):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise self.exc


def test_api_client_builds_direct_httpx_client_for_local_base_url() -> None:
    captured: dict[str, object] = {}

    def fake_client_factory(**kwargs):
        captured.update(kwargs)
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        proxy_decider=ProxyDecider(
            ProxyConfig(
                mode="socks5",
                proxy_url="socks5://127.0.0.1:1080",
                bypass_rules=["127.0.0.1"],
            )
        ),
        client_factory=fake_client_factory,
    )

    assert captured["trust_env"] is False
    assert "proxy" not in captured
    client.close()


def test_api_client_builds_manual_proxy_httpx_client_for_remote_base_url() -> None:
    captured: dict[str, object] = {}

    def fake_client_factory(**kwargs):
        captured.update(kwargs)
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))

    client = ApiClient(
        base_url="https://demo.remote.example",
        proxy_decider=ProxyDecider(
            ProxyConfig(
                mode="http",
                proxy_url="http://127.0.0.1:7890",
                bypass_rules=[],
            )
        ),
        client_factory=fake_client_factory,
    )

    assert captured["proxy"] == "http://127.0.0.1:7890"
    assert captured["trust_env"] is False
    client.close()


def test_api_client_attaches_authorization_header() -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers["authorization"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        transport=httpx.MockTransport(handler),
    )

    client.list_vod("1$%2F$1", page=1, size=25)

    assert seen_headers["authorization"] == "token-123"


def test_pull_playback_records_sends_source_filters() -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"items": [], "deleted": [], "nextSince": "1"})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        transport=httpx.MockTransport(handler),
    )

    client.pull_playback_records(
        1,
        source_kinds="site,spider_plugin",
        site_keys="csp_AList,csp_TgWeb",
    )

    assert seen_headers["x-playsync-source-kind"] == "site,spider_plugin"
    assert seen_headers["x-playsync-site-key"] == "csp_AList,csp_TgWeb"


def test_api_client_uses_vod_token_for_vod_requests() -> None:
    seen_path = {"value": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_path["value"] = request.url.path
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        vod_token="vod-123",
        transport=httpx.MockTransport(handler),
    )

    client.list_vod("1$%2F$1", page=1, size=25)

    assert seen_path["value"] == "/vod/vod-123"


def test_api_client_search_alist_items_uses_vod_token_and_keyword_param() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["wd"] = request.url.params.get("wd")
        seen["ac"] = request.url.params.get("ac")
        seen["pg"] = request.url.params.get("pg")
        return httpx.Response(200, json={"list": [], "pagecount": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        vod_token="vod-123",
        transport=httpx.MockTransport(handler),
    )

    client.search_alist_items("疑犯追踪")

    assert seen["path"] == "/vod/vod-123"
    assert seen["wd"] == "疑犯追踪"
    assert seen["ac"] == "gui"
    assert seen["pg"] is None


def test_api_client_search_alist_items_includes_page_param_after_first_page() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["pg"] = request.url.params.get("pg")
        return httpx.Response(200, json={"list": [], "pagecount": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        vod_token="vod-123",
        transport=httpx.MockTransport(handler),
    )

    client.search_alist_items("疑犯追踪", page=2)

    assert seen["pg"] == "2"


def test_api_client_raises_unauthorized_error_for_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="bad-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(UnauthorizedError):
        client.list_vod("1$%2F$1", page=1, size=25)


def test_api_client_raises_api_error_for_non_401_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ApiError) as exc:
        client.telegram_search("movie")

    assert str(exc.value) == "boom"


def test_api_client_maps_history_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 1,
                "key": "movie-1",
                "vodName": "Movie",
                "vodPic": "pic",
                "vodRemarks": "Episode 2",
                "episode": 1,
                "episodeUrl": "2.m3u8",
                "position": 90000,
                "opening": 0,
                "ending": 0,
                "speed": 1.25,
                "createTime": 123456,
                "sourceSubgroupIndex": 2,
                "sourceSubgroupName": "第三季",
                "driveDirId": "season-3",
            },
        )

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        transport=httpx.MockTransport(handler),
    )

    history = client.get_history("movie-1")

    assert isinstance(history, HistoryRecord)
    assert history.key == "movie-1"
    assert history.speed == 1.25
    assert history.source_subgroup_index == 2
    assert history.source_subgroup_name == "第三季"
    assert history.drive_dir_id == "season-3"


def test_api_client_maps_history_record_when_optional_fields_are_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 1,
                "key": "1$101286$1",
                "vodName": "Movie",
                "episode": 0,
                "episodeUrl": "https://media.example/1.m3u8",
                "position": 90000,
                "opening": 0,
                "ending": 0,
                "speed": 1.0,
                "createTime": 123456,
            },
        )

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        transport=httpx.MockTransport(handler),
    )

    history = client.get_history("1$101286$1")

    assert isinstance(history, HistoryRecord)
    assert history.key == "1$101286$1"
    assert history.vod_pic == ""
    assert history.vod_remarks == ""
    assert history.playlist_index == 0


def test_api_client_get_history_reads_grouped_source_indexes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 1,
                "key": "movie-1",
                "vodName": "Movie",
                "episode": 2,
                "episodeUrl": "https://b2/3.m3u8",
                "playlistIndex": 4,
                "sourceGroupIndex": 2,
                "sourceIndex": 1,
                "createTime": 123,
            },
        )

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        transport=httpx.MockTransport(handler),
    )

    history = client.get_history("movie-1")

    assert history is not None
    assert history.playlist_index == 4
    assert history.source_group_index == 2
    assert history.source_index == 1


def test_api_client_fetches_vod_token_from_api_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "vod-123,backup"})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        transport=httpx.MockTransport(handler),
    )

    assert client.fetch_vod_token() == "vod-123"


def test_api_client_gets_capabilities() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"emby": True, "jellyfin": False, "pansou": True})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        transport=httpx.MockTransport(handler),
    )

    capabilities = client.get_capabilities()

    assert capabilities == {"emby": True, "jellyfin": False, "pansou": True}
    assert seen == {"path": "/api/capabilities", "query": ""}


def test_api_client_gets_capabilities_with_feiniu() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"emby": True, "jellyfin": False, "feiniu": True, "pansou": True})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        transport=httpx.MockTransport(handler),
    )

    assert client.get_capabilities()["feiniu"] is True


def test_api_client_gets_capabilities_with_bilibili() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bilibili": True})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        transport=httpx.MockTransport(handler),
    )

    assert client.get_capabilities()["bilibili"] is True


def test_api_client_gets_video_cover_setting() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"name": "video_cover", "value": "https://img.example/cover.jpg"})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        transport=httpx.MockTransport(handler),
    )

    assert client.get_video_cover() == "https://img.example/cover.jpg"
    assert seen == {"path": "/api/settings/video_cover", "query": ""}


def test_api_client_renames_video() -> None:
    seen = {"method": "", "path": "", "json": None}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["json"] = request.read().decode()
        return httpx.Response(200, json={})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        transport=httpx.MockTransport(handler),
    )

    client.rename_video("91483", "新名.mkv")

    assert seen == {"method": "POST", "path": "/api/videos/91483/rename", "json": '{"name":"新名.mkv"}'}


def test_api_client_deletes_video() -> None:
    seen = {"method": "", "path": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        transport=httpx.MockTransport(handler),
    )

    client.delete_video("91483")

    assert seen == {"method": "DELETE", "path": "/api/videos/91483"}


def test_api_client_get_video_cover_returns_empty_string_for_missing_value() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "video_cover", "value": None})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        transport=httpx.MockTransport(handler),
    )

    assert client.get_video_cover() == ""


def test_api_client_returns_none_for_successful_empty_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, content=b"")

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        vod_token="vod-123",
        transport=httpx.MockTransport(handler),
    )

    assert client.logout() is None


def test_api_client_returns_plain_text_for_successful_text_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="/电影/国产")

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        vod_token="vod-123",
        transport=httpx.MockTransport(handler),
    )

    assert client.resolve_share_link("https://t.me/share") == "/电影/国产"


def test_api_client_get_text_returns_text_response() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="#EXTM3U")

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        transport=httpx.MockTransport(handler),
    )

    text = client.get_text("https://example.com/live.m3u")

    assert text == "#EXTM3U"
    assert seen == ["https://example.com/live.m3u"]


def test_api_client_get_bytes_returns_raw_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x1f\x8bcompressed")

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        transport=httpx.MockTransport(handler),
    )

    assert client.get_bytes("https://example.com/e9.xml.gz") == b"\x1f\x8bcompressed"


def test_api_client_close_closes_underlying_http_client() -> None:
    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
    )

    client.close()

    assert client._client.is_closed is True


def test_api_client_maps_file_list_read_timeout_to_localized_api_error() -> None:
    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="vod-123",
        transport=RaisingTransport(httpx.ReadTimeout("timed out")),
    )

    with pytest.raises(ApiError) as exc:
        client.list_vod("1$/电影$1", page=1, size=50)

    assert str(exc.value) == "加载文件列表超时"


def test_api_client_maps_non_file_list_timeout_to_generic_timeout_error() -> None:
    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="vod-123",
        transport=RaisingTransport(httpx.ConnectTimeout("timed out")),
    )

    with pytest.raises(ApiError) as exc:
        client.telegram_search("movie")

    assert str(exc.value) == "请求超时"


def test_api_client_maps_transport_http_error_to_network_request_failed() -> None:
    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="vod-123",
        transport=RaisingTransport(httpx.HTTPError("boom")),
    )

    with pytest.raises(ApiError) as exc:
        client.telegram_search("movie")

    assert str(exc.value) == "网络请求失败"


def test_api_client_logs_request_start_without_sensitive_payload(caplog) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"token": "vod-1"}))
    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="secret-token",
        transport=transport,
    )

    with caplog.at_level(logging.INFO):
        client.login("alice", "super-secret")

    assert "API request" in caplog.text
    assert "/api/accounts/login" in caplog.text
    assert "secret-token" not in caplog.text
    assert "super-secret" not in caplog.text


def test_api_client_logs_request_failure(caplog) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        transport=transport,
    )

    with caplog.at_level(logging.INFO):
        with pytest.raises(ApiError):
            client.get_capabilities()

    assert "API request failed" in caplog.text
    assert "/api/capabilities" in caplog.text


def test_api_client_lists_douban_categories() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"class": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_douban_categories()

    assert seen == {"path": "/tg-db/Harold", "query": ""}


def test_api_client_lists_douban_items() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_douban_items("movie", page=1, size=30)
    client.list_douban_items("movie", page=3, size=30)

    assert seen_queries == ["ac=gui&t=movie&pg=1&size=30", "ac=gui&t=movie&pg=3&size=30"]


def test_api_client_lists_douban_items_with_filters() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_douban_items("movie", page=2, size=30, filters={"sc": "6", "status": "1"})

    assert seen_queries == ["ac=gui&t=movie&pg=2&size=30&sc=6&status=1"]


def test_metadata_douban_search_requests_backend_endpoint() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"items": []})

    client = ApiClient(
        "http://127.0.0.1:4567",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    payload = client.search_douban_metadata("深空彼岸", year="2026")

    assert payload == {"items": []}
    assert seen == {
        "path": "/api/movies",
        "query": "q=%E6%B7%B1%E7%A9%BA%E5%BD%BC%E5%B2%B8",
    }


def test_metadata_douban_detail_requests_backend_endpoint() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"dbid": 35746415})

    client = ApiClient(
        "http://127.0.0.1:4567",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    payload = client.get_douban_metadata_detail(35746415)

    assert payload == {"dbid": 35746415}
    assert seen == {
        "path": "/api/movies/35746415",
        "query": "",
    }


def test_api_client_lists_telegram_search_categories() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"class": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_telegram_search_categories()

    assert seen == {"path": "/tg-search/Harold", "query": "web=true"}


def test_api_client_lists_telegram_search_items() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_telegram_search_items("0", page=1)
    client.list_telegram_search_items("XiangxiuNBB", page=2)

    assert seen_queries == ["t=0&web=true", "t=XiangxiuNBB&web=true&pg=2"]


def test_api_client_gets_telegram_search_detail() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"list": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_telegram_search_detail("https://pan.quark.cn/s/f518510ef92a")

    assert seen == {
        "path": "/tg-search/Harold",
        "query": "id=https%3A%2F%2Fpan.quark.cn%2Fs%2Ff518510ef92a&ac=gui",
    }


def test_api_client_gets_drive_share_detail() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"list": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_drive_share_detail("https://pan.quark.cn/s/f518510ef92a")

    assert seen == {
        "path": "/tg-search/Harold",
        "query": "id=https%3A%2F%2Fpan.quark.cn%2Fs%2Ff518510ef92a&ac=gui",
    }


def test_api_client_posts_offline_download_detail() -> None:
    seen = {"path": "", "query": "", "method": "", "body": b""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        seen["method"] = request.method
        seen["body"] = request.content
        return httpx.Response(200, json={"list": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_offline_download_detail("magnet:?xt=urn:btih:8a06396e03acb19d72eb2d779a22b2dc00f66a33")

    assert seen == {
        "path": "/offline_download/Harold",
        "query": "ac=gui",
        "method": "POST",
        "body": b'{"url":"magnet:?xt=urn:btih:8a06396e03acb19d72eb2d779a22b2dc00f66a33"}',
    }


def test_api_client_searches_telegram_items_by_keyword() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.search_telegram_items("黑袍纠察队", page=1)
    client.search_telegram_items("黑袍纠察队", page=3)

    assert seen_queries == [
        "web=true&wd=%E9%BB%91%E8%A2%8D%E7%BA%A0%E5%AF%9F%E9%98%9F",
        "web=true&wd=%E9%BB%91%E8%A2%8D%E7%BA%A0%E5%AF%9F%E9%98%9F&pg=3",
    ]


def test_api_client_uses_tgsc_endpoints_for_telegram_channel() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.url.query.decode()))
        return httpx.Response(200, json={"list": [], "class": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_telegram_channel_categories()
    client.list_telegram_channel_items("0", page=1)
    client.list_telegram_channel_items("Movie", page=2)
    client.get_telegram_channel_detail("tg-channel-vod-1")
    client.search_telegram_channel_items("黑袍纠察队", page=3)

    assert seen == [
        ("/tgsc/Harold", ""),
        ("/tgsc/Harold", "t=0"),
        ("/tgsc/Harold", "t=Movie&pg=2"),
        ("/tgsc/Harold", "id=tg-channel-vod-1&ac=gui"),
        ("/tgsc/Harold", "wd=%E9%BB%91%E8%A2%8D%E7%BA%A0%E5%AF%9F%E9%98%9F&pg=3"),
    ]


def test_api_client_lists_live_categories() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"class": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_live_categories()

    assert seen == {"path": "/live/Harold", "query": ""}


def test_api_client_lists_live_items() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_live_items("bili", page=1)
    client.list_live_items("bili-9", page=1)
    client.list_live_items("bili-9-744", page=2)

    assert seen_queries == [
        "t=bili&ac=gui&pg=1",
        "t=bili-9&ac=gui&pg=1",
        "t=bili-9-744&ac=gui&pg=2",
    ]


def test_api_client_gets_live_detail_by_ids() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"list": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_live_detail("bili$1785607569")

    assert seen == {"path": "/live/Harold", "query": "ids=bili%241785607569&platform=gui"}


def test_api_client_lists_emby_categories() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"class": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_emby_categories()

    assert seen == {"path": "/emby/Harold", "query": ""}


def test_api_client_lists_emby_items() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_emby_items("Series", page=1)
    client.list_emby_items("Series", page=3)

    assert seen_queries == ["t=Series&pg=1", "t=Series&pg=3"]


def test_api_client_lists_emby_items_with_filters() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_emby_items("Series", page=2, filters={"status": "1", "sc": "6"})

    assert seen_queries == ["t=Series&pg=2&status=1&sc=6"]


def test_api_client_searches_emby_items_by_keyword() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.search_emby_items("黑袍纠察队", page=1)
    client.search_emby_items("黑袍纠察队", page=2)

    assert seen_queries == [
        "wd=%E9%BB%91%E8%A2%8D%E7%BA%A0%E5%AF%9F%E9%98%9F",
        "wd=%E9%BB%91%E8%A2%8D%E7%BA%A0%E5%AF%9F%E9%98%9F&pg=2",
    ]


def test_api_client_gets_emby_detail_by_ids() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"list": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_emby_detail("1-3281")

    assert seen == {"path": "/emby/Harold", "query": "ids=1-3281"}


def test_api_client_gets_emby_playback_source() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"url": ["Episode 1", "http://m/1.mp4"], "header": {"User-Agent": "Yamby"}})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_emby_playback_source("1-3458")

    assert seen == {"path": "/emby-play/Harold", "query": "t=0&id=1-3458"}


def test_api_client_reports_emby_playback_progress() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"ok": True})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.report_emby_playback_progress("1-3458", 1000)

    assert seen == {"path": "/emby-play/Harold", "query": "t=1000&id=1-3458"}


def test_api_client_stops_emby_playback() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"ok": True})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.stop_emby_playback("1-3458")

    assert seen == {"path": "/emby-play/Harold", "query": "t=-1&id=1-3458"}


def test_api_client_lists_feiniu_categories() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"class": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_feiniu_categories()

    assert seen == {"path": "/feiniu/Harold", "query": ""}


def test_api_client_lists_bilibili_categories() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"class": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_bilibili_categories()

    assert seen == {"path": "/bilibili/Harold", "query": ""}


def test_api_client_lists_bilibili_items_with_filters() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_bilibili_items("bangumi", page=2, filters={"season_status": "1"})

    assert seen_queries == ["t=bangumi&pg=2&season_status=1"]


def test_api_client_gets_bilibili_detail_by_ids() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"list": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_bilibili_detail("BV1xx411c7mD")

    assert seen == {"path": "/bilibili/Harold", "query": "ids=BV1xx411c7mD"}


def test_api_client_gets_bilibili_playback_source() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"url": "https://stream.example/1.m3u8"})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_bilibili_playback_source("BV1xx411c7mD")

    assert seen == {"path": "/play/Harold", "query": "bvid=BV1xx411c7mD&dash=true"}


def test_api_client_gets_feiniu_playback_source() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"url": "https://stream.example/1.m3u8"})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_feiniu_playback_source("1-5001")

    assert seen == {"path": "/feiniu-play/Harold", "query": "t=0&id=1-5001"}


def test_api_client_lists_jellyfin_categories() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"class": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_jellyfin_categories()

    assert seen == {"path": "/jellyfin/Harold", "query": ""}


def test_api_client_lists_jellyfin_items() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_jellyfin_items("Series", page=1)
    client.list_jellyfin_items("Series", page=3)

    assert seen_queries == ["t=Series&pg=1", "t=Series&pg=3"]


def test_api_client_lists_jellyfin_items_with_filters() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_jellyfin_items("Series", page=2, filters={"status": "1", "sc": "6"})

    assert seen_queries == ["t=Series&pg=2&status=1&sc=6"]


def test_api_client_searches_jellyfin_items_by_keyword() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.search_jellyfin_items("人生切割术", page=1)
    client.search_jellyfin_items("人生切割术", page=2)

    assert seen_queries == [
        "wd=%E4%BA%BA%E7%94%9F%E5%88%87%E5%89%B2%E6%9C%AF",
        "wd=%E4%BA%BA%E7%94%9F%E5%88%87%E5%89%B2%E6%9C%AF&pg=2",
    ]


def test_api_client_gets_jellyfin_detail_by_ids() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"list": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_jellyfin_detail("1-3281")

    assert seen == {"path": "/jellyfin/Harold", "query": "ids=1-3281"}


def test_api_client_gets_jellyfin_playback_source() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"url": ["Episode 1", "http://j/1.mp4"], "header": {"User-Agent": "Jellyfin"}})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_jellyfin_playback_source("1-3458")

    assert seen == {"path": "/jellyfin-play/Harold", "query": "t=0&id=1-3458"}


def test_api_client_reports_jellyfin_playback_progress() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"ok": True})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.report_jellyfin_playback_progress("1-3458", 1000)

    assert seen == {"path": "/jellyfin-play/Harold", "query": "t=1000&id=1-3458"}


def test_api_client_stops_jellyfin_playback() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"ok": True})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.stop_jellyfin_playback("1-3458")

    assert seen == {"path": "/jellyfin-play/Harold", "query": "t=-1&id=1-3458"}


def test_api_client_lists_media_subscriptions() -> None:
    seen = {"path": "", "auth": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json=[{"id": 1, "name": "凡人修仙传", "metaProvider": "tmdb", "metaId": "456"}])

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="session-token",
        transport=httpx.MockTransport(handler),
    )

    subscriptions = client.list_media_subscriptions()

    assert seen["path"] == "/api/media-subscriptions"
    assert seen["auth"] == "session-token"
    assert subscriptions[0]["metaId"] == "456"


def test_api_client_creates_media_subscription() -> None:
    seen = {"path": "", "method": "", "body": None}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"id": 9})

    client = ApiClient(base_url="http://127.0.0.1:4567", transport=httpx.MockTransport(handler))

    payload = client.create_media_subscription({"name": "凡人修仙传", "season": 1, "metaProvider": "tmdb", "metaId": "456"})

    assert seen == {
        "path": "/api/media-subscriptions",
        "method": "POST",
        "body": {"name": "凡人修仙传", "season": 1, "metaProvider": "tmdb", "metaId": "456"},
    }
    assert payload == {"id": 9}


def test_api_client_gets_media_subscription_detail() -> None:
    seen = {"path": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"subscription": {"id": 5}, "media": {}, "episodes": []})

    client = ApiClient(base_url="http://127.0.0.1:4567", transport=httpx.MockTransport(handler))

    detail = client.get_media_subscription_detail(5)

    assert seen["path"] == "/api/media-subscriptions/5/detail"
    assert detail["subscription"]["id"] == 5


def test_api_client_resolves_msub_episode_preferring_username_token() -> None:
    seen = {"path": "", "query": "", "client": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        seen["client"] = request.headers.get("X-CLIENT", "")
        return httpx.Response(200, json={"url": "http://d/1.mkv", "type": "alias", "header": {}})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        vod_token="vod-token",
        username="harold",
        transport=httpx.MockTransport(handler),
    )

    payload = client.resolve_msub_episode(5, 3)

    # 无 token 路径会被 checkToken("") 拒绝;用户名令牌精确映射到本人 uid
    assert seen["path"] == "/play/harold"
    assert seen["query"] == "id=msubep-5-3"
    assert seen["client"] == "atv-player"
    assert payload["url"] == "http://d/1.mkv"


def test_api_client_resolves_msub_episode_falls_back_to_vod_token() -> None:
    seen = {"path": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"url": "http://d/1.mkv"})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        vod_token="vod-token",
        transport=httpx.MockTransport(handler),
    )

    client.resolve_msub_episode(5, 3)

    assert seen["path"] == "/play/vod-token"


def test_api_client_resolves_msub_episode_surfaces_failure_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "第 3 集暂无可用播放源(已尝试 2 个源)", "status": 400})

    client = ApiClient(base_url="http://127.0.0.1:4567", transport=httpx.MockTransport(handler))

    with pytest.raises(ApiError) as excinfo:
        client.resolve_msub_episode(5, 3)
    assert "暂无可用播放源" in str(excinfo.value)
