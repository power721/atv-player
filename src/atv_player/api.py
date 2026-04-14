from __future__ import annotations

from typing import Any

import httpx

from atv_player.models import HistoryRecord


class ApiError(RuntimeError):
    pass


class UnauthorizedError(ApiError):
    pass


class ApiClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": token} if token else {}
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=30.0,
        )

    def set_token(self, token: str) -> None:
        if token:
            self._client.headers["Authorization"] = token
        else:
            self._client.headers.pop("Authorization", None)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self._client.request(method, url, **kwargs)
        if response.status_code == 401:
            raise UnauthorizedError("Unauthorized")
        if response.is_error:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise ApiError(payload.get("message") or payload.get("detail") or response.text)
        return response.json()

    def login(self, username: str, password: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/accounts/login",
            json={"username": username, "password": password},
        )

    def list_vod(self, path_id: str, page: int, size: int) -> dict[str, Any]:
        token = self._client.headers.get("Authorization", "")
        return self._request(
            "GET",
            f"/vod/{token}",
            params={"ac": "web", "pg": page, "size": size, "t": path_id},
        )

    def get_detail(self, vod_id: str) -> dict[str, Any]:
        token = self._client.headers.get("Authorization", "")
        return self._request(
            "GET",
            f"/vod/{token}",
            params={"ac": "web", "ids": vod_id},
        )

    def telegram_search(self, keyword: str) -> dict[str, Any]:
        return self._request("GET", "/api/telegram/search", params={"wd": keyword})

    def resolve_share_link(self, link: str) -> str:
        data = self._request(
            "POST",
            "/api/share-link",
            json={"link": link, "path": "", "code": ""},
        )
        return str(data)

    def get_history(self, key: str) -> HistoryRecord | None:
        token = self._client.headers.get("Authorization", "")
        data = self._request("GET", f"/history/{token}", params={"key": key})
        if not data:
            return None
        return HistoryRecord(
            id=int(data["id"]),
            key=str(data["key"]),
            vod_name=str(data["vodName"]),
            vod_pic=str(data["vodPic"]),
            vod_remarks=str(data["vodRemarks"]),
            episode=int(data["episode"]),
            episode_url=str(data["episodeUrl"]),
            position=int(data["position"]),
            opening=int(data["opening"]),
            ending=int(data["ending"]),
            speed=float(data["speed"]),
            create_time=int(data["createTime"]),
        )

    def list_history(self, page: int, size: int) -> dict[str, Any]:
        return self._request(
            "GET",
            "/api/history",
            params={"sort": "createTime,desc", "page": page - 1, "size": size},
        )

    def save_history(self, payload: dict[str, Any]) -> None:
        self._request("POST", "/api/history", params={"log": "false"}, json=payload)

    def delete_history(self, history_id: int) -> None:
        self._request("DELETE", f"/api/history/{history_id}")

    def delete_histories(self, history_ids: list[int]) -> None:
        self._request("POST", "/api/history/-/delete", json=history_ids)

    def clear_history(self) -> None:
        token = self._client.headers.get("Authorization", "")
        self._request("DELETE", f"/history/{token}")
