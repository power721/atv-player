import logging

import httpx
import pytest

from atv_player.api import ApiClient, ApiError, UnauthorizedError
from atv_player.models import HistoryRecord


class RaisingTransport(httpx.BaseTransport):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise self.exc


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


def test_api_client_treats_successful_empty_delete_response_as_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return httpx.Response(204, content=b"")

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        vod_token="vod-123",
        transport=httpx.MockTransport(handler),
    )

    assert client.delete_history(9) is None


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

    assert seen == {"path": "/live/Harold", "query": "ids=bili%241785607569"}


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
