from __future__ import annotations

import logging
from typing import Any

import httpx

from atv_player.models import HistoryRecord


class ApiError(RuntimeError):
    pass


class UnauthorizedError(ApiError):
    pass


logger = logging.getLogger(__name__)


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

    def close(self) -> None:
        self._client.close()

    def _is_file_list_request(self, url: str, params: Any) -> bool:
        if not url.startswith("/vod/"):
            return False
        if not isinstance(params, dict):
            return False
        return params.get("ac") == "gui" and "t" in params

    def _summarize_params(self, params: Any) -> dict[str, Any] | None:
        if not isinstance(params, dict):
            return None
        summary: dict[str, Any] = {}
        for key, value in params.items():
            if key.lower() in {"token", "authorization", "password"}:
                continue
            if key in {"wd", "t", "id", "ids", "pg", "size", "ac", "web", "sort", "page"}:
                summary[str(key)] = value
        return summary or None

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        logger.info(
            "API request method=%s url=%s params=%s",
            method,
            url,
            self._summarize_params(kwargs.get("params")),
        )
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.ReadTimeout as exc:
            logger.warning("API request timeout method=%s url=%s", method, url, exc_info=exc)
            if self._is_file_list_request(url, kwargs.get("params")):
                raise ApiError("加载文件列表超时") from exc
            raise ApiError("请求超时") from exc
        except httpx.TimeoutException as exc:
            logger.warning("API request timeout method=%s url=%s", method, url, exc_info=exc)
            raise ApiError("请求超时") from exc
        except httpx.HTTPError as exc:
            logger.warning("API request transport error method=%s url=%s", method, url, exc_info=exc)
            raise ApiError("网络请求失败") from exc
        if response.status_code == 401:
            logger.warning("API request unauthorized method=%s url=%s", method, url)
            raise UnauthorizedError("Unauthorized")
        if response.is_error:
            logger.warning(
                "API request failed method=%s url=%s status=%s",
                method,
                url,
                response.status_code,
            )
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

    def list_douban_items(
        self,
        category_id: str,
        page: int,
        size: int = 35,
        filters: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"ac": "gui", "t": category_id, "pg": page, "size": size}
        if filters:
            params.update(filters)
        return self._request(
            "GET",
            f"/tg-db/{self._vod_token}",
            params=params,
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

    def get_drive_share_detail(self, link: str) -> dict[str, Any]:
        return self._request("GET", f"/tg-search/{self._vod_token}", params={"id": link, "ac": "gui"})

    def search_telegram_items(self, keyword: str, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"web": True, "wd": keyword}
        if page > 1:
            params["pg"] = page
        return self._request("GET", f"/tg-search/{self._vod_token}", params=params)

    def list_live_categories(self) -> dict[str, Any]:
        return self._request("GET", f"/live/{self._vod_token}")

    def list_live_items(self, category_id: str, page: int) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/live/{self._vod_token}",
            params={"t": category_id, "ac": "gui", "pg": page},
        )

    def get_live_detail(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/live/{self._vod_token}", params={"ids": vod_id})

    def list_emby_categories(self) -> dict[str, Any]:
        return self._request("GET", f"/emby/{self._vod_token}")

    def list_emby_items(
        self,
        category_id: str,
        page: int,
        filters: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"t": category_id, "pg": page}
        if filters:
            params.update(filters)
        return self._request(
            "GET",
            f"/emby/{self._vod_token}",
            params=params,
        )

    def search_emby_items(self, keyword: str, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"wd": keyword}
        if page > 1:
            params["pg"] = page
        return self._request("GET", f"/emby/{self._vod_token}", params=params)

    def get_emby_detail(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/emby/{self._vod_token}", params={"ids": vod_id})

    def get_emby_playback_source(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/emby-play/{self._vod_token}", params={"t": 0, "id": vod_id})

    def report_emby_playback_progress(self, vod_id: str, position_ms: int) -> None:
        self._request("GET", f"/emby-play/{self._vod_token}", params={"t": position_ms, "id": vod_id})

    def stop_emby_playback(self, vod_id: str) -> None:
        self._request("GET", f"/emby-play/{self._vod_token}", params={"t": -1, "id": vod_id})

    def list_feiniu_categories(self) -> dict[str, Any]:
        return self._request("GET", f"/feiniu/{self._vod_token}")

    def list_feiniu_items(
        self,
        category_id: str,
        page: int,
        filters: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"t": category_id, "pg": page}
        if filters:
            params.update(filters)
        return self._request(
            "GET",
            f"/feiniu/{self._vod_token}",
            params=params,
        )

    def search_feiniu_items(self, keyword: str, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"wd": keyword}
        if page > 1:
            params["pg"] = page
        return self._request("GET", f"/feiniu/{self._vod_token}", params=params)

    def get_feiniu_detail(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/feiniu/{self._vod_token}", params={"ids": vod_id})

    def get_feiniu_playback_source(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/feiniu-play/{self._vod_token}", params={"t": 0, "id": vod_id})

    def report_feiniu_playback_progress(self, vod_id: str, position_ms: int) -> None:
        self._request("GET", f"/feiniu-play/{self._vod_token}", params={"t": position_ms, "id": vod_id})

    def stop_feiniu_playback(self, vod_id: str) -> None:
        self._request("GET", f"/feiniu-play/{self._vod_token}", params={"t": -1, "id": vod_id})

    def list_jellyfin_categories(self) -> dict[str, Any]:
        return self._request("GET", f"/jellyfin/{self._vod_token}")

    def list_jellyfin_items(
        self,
        category_id: str,
        page: int,
        filters: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"t": category_id, "pg": page}
        if filters:
            params.update(filters)
        return self._request(
            "GET",
            f"/jellyfin/{self._vod_token}",
            params=params,
        )

    def search_jellyfin_items(self, keyword: str, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"wd": keyword}
        if page > 1:
            params["pg"] = page
        return self._request("GET", f"/jellyfin/{self._vod_token}", params=params)

    def get_jellyfin_detail(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/jellyfin/{self._vod_token}", params={"ids": vod_id})

    def get_jellyfin_playback_source(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/jellyfin-play/{self._vod_token}", params={"t": 0, "id": vod_id})

    def report_jellyfin_playback_progress(self, vod_id: str, position_ms: int) -> None:
        self._request("GET", f"/jellyfin-play/{self._vod_token}", params={"t": position_ms, "id": vod_id})

    def stop_jellyfin_playback(self, vod_id: str) -> None:
        self._request("GET", f"/jellyfin-play/{self._vod_token}", params={"t": -1, "id": vod_id})

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
            vod_name=str(data.get("vodName") or ""),
            vod_pic=str(data.get("vodPic") or ""),
            vod_remarks=str(data.get("vodRemarks") or ""),
            episode=int(data.get("episode", 0)),
            episode_url=str(data.get("episodeUrl") or ""),
            position=int(data.get("position", 0)),
            opening=int(data.get("opening", 0)),
            ending=int(data.get("ending", 0)),
            speed=float(data.get("speed", 1.0)),
            create_time=int(data.get("createTime", 0)),
            playlist_index=int(data.get("playlistIndex", 0)),
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

    def get_capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/api/capabilities")

    def get_text(self, url: str) -> str:
        logger.info("API text request url=%s", url)
        try:
            response = self._client.get(url, follow_redirects=True)
            response.raise_for_status()
        except httpx.ReadTimeout as exc:
            logger.warning("API text request timeout url=%s", url, exc_info=exc)
            raise ApiError("请求超时") from exc
        except httpx.TimeoutException as exc:
            logger.warning("API text request timeout url=%s", url, exc_info=exc)
            raise ApiError("请求超时") from exc
        except httpx.HTTPError as exc:
            logger.warning("API text request failed url=%s", url, exc_info=exc)
            raise ApiError("网络请求失败") from exc
        return response.text

    def get_bytes(self, url: str) -> bytes:
        logger.info("API bytes request url=%s", url)
        try:
            response = self._client.get(url, follow_redirects=True)
            response.raise_for_status()
        except httpx.ReadTimeout as exc:
            logger.warning("API bytes request timeout url=%s", url, exc_info=exc)
            raise ApiError("请求超时") from exc
        except httpx.TimeoutException as exc:
            logger.warning("API bytes request timeout url=%s", url, exc_info=exc)
            raise ApiError("请求超时") from exc
        except httpx.HTTPError as exc:
            logger.warning("API bytes request failed url=%s", url, exc_info=exc)
            raise ApiError("网络请求失败") from exc
        return response.content
