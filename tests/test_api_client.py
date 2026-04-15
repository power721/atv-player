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


def test_api_client_fetches_vod_token_from_api_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "vod-123,backup"})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        transport=httpx.MockTransport(handler),
    )

    assert client.fetch_vod_token() == "vod-123"


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
