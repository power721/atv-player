from __future__ import annotations

import hashlib
import logging
import platform
import urllib.parse
from collections.abc import Callable
from typing import Any

import httpx

from atv_player.models import HistoryRecord
from atv_player.network_proxy import ProxyDecider, build_httpx_kwargs_for_url


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
        proxy_decider: ProxyDecider | None = None,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
        username: str = "",
    ) -> None:
        self._base_url = base_url
        # 令牌随每次登录轮换,而用户名稳定。身份按用户名派生,使同步游标/快照跨会话保留,
        # 避免每次登录全量重拉重推;用户名为空时退化回令牌派生(旧行为)。
        self._username = username or ""
        self._playback_sync_identity = self._build_playback_sync_identity(token)
        headers = {"Authorization": token} if token else {}
        headers.setdefault("User-Agent", platform.platform() + " ATV-Player")
        self._vod_token = vod_token
        client_kwargs: dict[str, Any] = dict(
            base_url=base_url,
            headers=headers,
            transport=transport,
            timeout=30.0,
        )
        client_kwargs.update(build_httpx_kwargs_for_url(proxy_decider, base_url))
        self._client = client_factory(**client_kwargs)

    def set_token(self, token: str) -> None:
        self._playback_sync_identity = self._build_playback_sync_identity(token)
        if token:
            self._client.headers["Authorization"] = token
        else:
            self._client.headers.pop("Authorization", None)

    @property
    def playback_sync_identity(self) -> str:
        return self._playback_sync_identity

    @property
    def username(self) -> str:
        return self._username

    @property
    def base_url(self) -> str:
        return self._base_url

    def _build_playback_sync_identity(self, token: str) -> str:
        stable = self._username or token
        value = f"{self._base_url}\n{stable}".encode()
        return hashlib.sha256(value).hexdigest()[:32]

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

    def logout(self) -> None:
        return self._request("POST", "/api/accounts/logout")

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
            params={"ac": "gui", "ids": vod_id, "depth": 1},
        )

    def search_alist_items(self, keyword: str, page: int = 1) -> dict[str, Any]:
        params: dict[str, Any] = {"ac": "gui", "wd": keyword}
        if page > 1:
            params["pg"] = page
        return self._request("GET", f"/vod/{self._vod_token}", params=params)

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

    def search_douban_metadata(self, title: str, year: str = "") -> dict[str, Any]:
        del year
        return self._request("GET", "/api/movies", params={"q": title})

    def get_douban_metadata_detail(self, dbid: int | str) -> dict[str, Any]:
        return self._request("GET", f"/api/movies/{dbid}")

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

    def resolve_drive(self, source: str, title: str = "") -> dict[str, Any]:
        # New per-directory drive API (Authorization header, no vod-token path). Returns media
        # info + top-level directories (+ root files); sub-directory files are loaded lazily.
        return self._request("POST", "/api/drive/resolve", json={"source": source, "title": title})

    def list_drive_files(self, resource_id: str, dir_id: str) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/api/drive/{resource_id}/files", params={"dir": dir_id})
        if isinstance(payload, dict):
            return list(payload.get("files") or [])
        return []

    def search_telegram_items(self, keyword: str, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"web": True, "wd": keyword}
        if page > 1:
            params["pg"] = page
        return self._request("GET", f"/tg-search/{self._vod_token}", params=params)

    def list_telegram_channel_categories(self) -> dict[str, Any]:
        return self._request("GET", f"/tgsc/{self._vod_token}")

    def list_telegram_channel_items(self, category_id: str, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"t": category_id}
        if category_id != "0":
            params["pg"] = page
        return self._request("GET", f"/tgsc/{self._vod_token}", params=params)

    def get_telegram_channel_detail(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/tgsc/{self._vod_token}", params={"id": vod_id, "ac": "gui"})

    def search_telegram_channel_items(self, keyword: str, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"wd": keyword}
        if page > 1:
            params["pg"] = page
        return self._request("GET", f"/tgsc/{self._vod_token}", params=params)

    def get_offline_download_detail(self, link: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/offline_download/{self._vod_token}",
            params={"ac": "gui"},
            json={"url": link},
        )

    def list_live_categories(self) -> dict[str, Any]:
        return self._request("GET", f"/live/{self._vod_token}")

    def list_live_items(self, category_id: str, page: int) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/live/{self._vod_token}",
            params={"t": category_id, "ac": "gui", "pg": page},
        )

    def get_live_detail(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/live/{self._vod_token}", params={"ids": vod_id, "platform": "gui"})

    def list_emby_categories(self) -> dict[str, Any]:
        return self._request("GET", f"/emby/{self._vod_token}")

    def list_bilibili_categories(self) -> dict[str, Any]:
        return self._request("GET", f"/bilibili/{self._vod_token}")

    def list_bilibili_items(
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
            f"/bilibili/{self._vod_token}",
            params=params,
        )

    def search_bilibili_items(self, keyword: str, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"wd": keyword}
        if page > 1:
            params["pg"] = page
        return self._request("GET", f"/bilibili/{self._vod_token}", params=params)

    def get_bilibili_detail(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/bilibili/{self._vod_token}", params={"ids": vod_id}, headers={"X-CLIENT": "gui"})

    def get_bilibili_playback_source(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/play/{self._vod_token}", params={"bvid": vod_id, "dash": True}, headers={"X-CLIENT": "gui"})

    def run_bilibili_detail_action(self, vod_id: str, action_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/bilibili/{self._vod_token}/action",
            json={"id": vod_id, "action": action_id},
            headers={"X-CLIENT": "gui"},
        )

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

    def list_media_subscriptions(self) -> list[dict[str, Any]]:
        # 服务端追剧系统:返回当前用户全部订阅(含 metaProvider/metaId/doubanId 与
        # currentEpisodes=挂载资源实际可播集数)。需 ADMIN/USER 会话令牌。
        data = self._request("GET", "/api/media-subscriptions")
        return data if isinstance(data, list) else []

    def create_media_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
        # 后端按 (name, season) 幂等,重复创建会返回既有订阅并可能再次触发巡检,
        # 调用方需自带负缓存避免每轮同步都 POST。
        data = self._request("POST", "/api/media-subscriptions", json=payload)
        return data if isinstance(data, dict) else {}

    def get_media_subscription_detail(self, subscription_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/api/media-subscriptions/{subscription_id}/detail")
        return data if isinstance(data, dict) else {}

    def resolve_msub_episode(self, subscription_id: int, episode: int) -> dict[str, Any]:
        # /play 无 token 路径会被服务端 checkToken("") 直接 400,必须带路径 token:
        # 用户名令牌经 resolveUid 精确映射到本人;无用户名时退 vod token(解析到首个管理员)。
        token = urllib.parse.quote(self._username or self._vod_token or "-", safe="")
        data = self._request(
            "GET",
            f"/play/{token}",
            params={"id": f"msubep-{subscription_id}-{episode}"},
            headers={"X-CLIENT": "atv-player"},
        )
        return data if isinstance(data, dict) else {}

    def resolve_share_link(self, link: str) -> str:
        data = self._request(
            "POST",
            "/api/share-link",
            json={"link": link, "path": "", "code": ""},
        )
        return str(data)

    def rename_video(self, video_id: str, name: str) -> None:
        self._request("POST", f"/api/videos/{video_id}/rename", json={"name": name})

    def delete_video(self, video_id: str) -> None:
        self._request("DELETE", f"/api/videos/{video_id}")

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
            duration=int(data.get("duration", 0)),
            opening=int(data.get("opening", 0)),
            ending=int(data.get("ending", 0)),
            speed=float(data.get("speed", 1.0)),
            create_time=int(data.get("createTime", 0)),
            playlist_index=int(data.get("playlistIndex", 0)),
            source_group_index=int(data.get("sourceGroupIndex", 0)),
            source_index=int(data.get("sourceIndex", 0)),
            source_subgroup_index=int(data.get("sourceSubgroupIndex", 0)),
            source_subgroup_name=str(data.get("sourceSubgroupName") or ""),
            drive_dir_id=str(data.get("driveDirId") or ""),
            drive_share_key=str(data.get("driveShareKey") or ""),
            drive_path=str(data.get("drivePath") or ""),
        )

    def push_playback_events(self, records: list[dict[str, Any]]) -> None:
        # 多端播放记录同步:PUSH 本地 Tier-B 记录。Authorization(session)由客户端自动携带,
        # 服务端 resolveUid 经 session 路径解析为 uid。
        if not records:
            return
        self._request("POST", "/api/playback/events", json=records)

    def pull_playback_records(
        self,
        since: int,
        limit: int = 100,
        *,
        source_kinds: str = "",
        site_keys: str = "",
    ) -> dict[str, Any]:
        headers = {"X-PlaySync-Since": str(since), "X-PlaySync-Limit": str(limit)}
        if source_kinds:
            headers["X-PlaySync-Source-Kind"] = source_kinds
        if site_keys:
            headers["X-PlaySync-Site-Key"] = site_keys
        if since <= 0:
            headers["X-PlaySync-Latest"] = "true"
        data = self._request(
            "GET",
            "/api/playback/changes",
            headers=headers,
        )
        return data or {}

    def fetch_vod_token(self) -> str:
        data = self._request("GET", "/api/token")
        token = str(data.get("token") or "")
        first = token.split(",")[0] if token else "-"
        self._vod_token = first
        return first

    def get_capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/api/capabilities")

    def get_video_cover(self) -> str:
        data = self._request("GET", "/api/settings/video_cover")
        if not isinstance(data, dict):
            return ""
        return str(data.get("value") or "")

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
