import httpx
import pytest

from atv_player.api import ApiClient, ApiError, UnauthorizedError


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
