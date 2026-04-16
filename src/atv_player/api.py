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
        vod_token: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": token} if token else {}
        self._vod_token = vod_token
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

    def set_vod_token(self, vod_token: str) -> None:
        self._vod_token = vod_token

    def _is_file_list_request(self, url: str, params: Any) -> bool:
        if not url.startswith("/vod/"):
            return False
        if not isinstance(params, dict):
            return False
        return params.get("ac") == "gui" and "t" in params

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.ReadTimeout as exc:
            if self._is_file_list_request(url, kwargs.get("params")):
                raise ApiError("加载文件列表超时") from exc
            raise ApiError("请求超时") from exc
        except httpx.TimeoutException as exc:
            raise ApiError("请求超时") from exc
        except httpx.HTTPError as exc:
            raise ApiError("网络请求失败") from exc
        if response.status_code == 401:
            raise UnauthorizedError("Unauthorized")
        if response.is_error:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise ApiError(payload.get("message") or payload.get("detail") or response.text)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def login(self, username: str, password: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/accounts/login",
            json={"username": username, "password": password},
        )

    def list_vod(self, path_id: str, page: int, size: int) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/vod/{self._vod_token}",
            params={"ac": "gui", "pg": page, "size": size, "t": path_id},
        )

    def get_detail(self, vod_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/vod/{self._vod_token}",
            params={"ac": "gui", "ids": vod_id},
        )

    def list_douban_categories(self) -> dict[str, Any]:
        return self._request("GET", f"/tg-db/{self._vod_token}")

    def list_douban_items(self, category_id: str, page: int, size: int = 35) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/tg-db/{self._vod_token}",
            params={"ac": "gui", "t": category_id, "pg": page, "size": size},
        )

    def list_telegram_search_categories(self) -> dict[str, Any]:
        return self._request("GET", f"/tg-search/{self._vod_token}", params={"web": True})

    def list_telegram_search_items(self, category_id: str, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"t": category_id, "web": True}
        if category_id != "0":
            params["pg"] = page
        return self._request("GET", f"/tg-search/{self._vod_token}", params=params)

    def get_telegram_search_detail(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/tg-search/{self._vod_token}", params={"id": vod_id, "ac": "gui"})

    def search_telegram_items(self, keyword: str, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"web": True, "wd": keyword}
        if page > 1:
            params["pg"] = page
        return self._request("GET", f"/tg-search/{self._vod_token}", params=params)

    def list_emby_categories(self) -> dict[str, Any]:
        return self._request("GET", f"/emby/{self._vod_token}")

    def list_emby_items(self, category_id: str, page: int) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/emby/{self._vod_token}",
            params={"t": category_id, "pg": page},
        )

    def search_emby_items(self, keyword: str, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"wd": keyword}
        if page > 1:
            params["pg"] = page
        return self._request("GET", f"/emby/{self._vod_token}", params=params)

    def get_emby_detail(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/emby/{self._vod_token}", params={"ids": vod_id})

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
        data = self._request("GET", f"/history/{self._vod_token}", params={"key": key})
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
        self._request("DELETE", f"/history/{self._vod_token}")

    def fetch_vod_token(self) -> str:
        data = self._request("GET", "/api/token")
        token = str(data.get("token") or "")
        first = token.split(",")[0] if token else "-"
        self._vod_token = first
        return first
