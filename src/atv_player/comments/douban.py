from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from atv_player.danmaku.discovery.douban import DoubanDiscovery, DoubanSubject
from atv_player.network_proxy import ProxyDecider, build_httpx_kwargs_for_url

_REXXAR_BASE = "https://m.douban.com/rexxar/api/v2"
_PAGE_SIZE = 20

_HEADERS = {
    "Referer": "https://m.douban.com/movie/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    ),
    "Accept": "application/json",
}

CommentsLoader = Callable[[dict[str, object]], dict[str, object]]


class DoubanCommentsClient:
    """豆瓣 rexxar 短评(interests)只读客户端。

    电影条目走 /movie/{id}/interests、电视剧条目走 /tv/{id}/interests;
    电影路径对剧集条目会 302 重定向到 /tv/(follow_redirects 自动跟随),
    个别不重定向的条目按 404 再补一次 /tv/。头像 img*.doubanio.com 的
    防盗链由 ui.poster_loader 的默认 Referer 覆盖,这里无需处理。
    """

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        proxy_decider: ProxyDecider | None = None,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        client_kwargs: dict[str, Any] = dict(
            base_url=_REXXAR_BASE,
            transport=transport,
            timeout=10.0,
            follow_redirects=True,
        )
        client_kwargs.update(build_httpx_kwargs_for_url(proxy_decider, _REXXAR_BASE))
        self._client = client_factory(**client_kwargs)
        self._discovery = DoubanDiscovery(self._get_json, self._post_json)

    def list_interests(
        self, douban_id: str | int, kind: str = "movie", start: int = 0, count: int = _PAGE_SIZE
    ) -> dict[str, Any]:
        subject_id = quote(str(douban_id).strip())
        if not subject_id:
            raise ValueError("豆瓣条目 ID 为空")
        normalized_kind = "tv" if str(kind).strip().lower() == "tv" else "movie"
        response = self._client.get(
            f"/{normalized_kind}/{subject_id}/interests",
            params={"start": max(0, int(start)), "count": max(1, int(count))},
            headers=dict(_HEADERS),
        )
        if response.status_code == 404 and normalized_kind == "movie":
            # 已知是剧集(或电影路径 404):换 /tv/ 再试一次
            return self.list_interests(douban_id, "tv", start, count)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("豆瓣短评响应格式异常")
        return payload

    def search_subjects(self, keyword: str) -> list[DoubanSubject]:
        return self._discovery.search_subjects(str(keyword or "").strip())

    def _get_json(self, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("headers", dict(_HEADERS))
        return self._client.get(url, **kwargs)

    def _post_json(self, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("headers", dict(_HEADERS))
        return self._client.post(url, **kwargs)


def douban_kind_from_type_name(type_name: str) -> str:
    """rexxar 搜索结果的 type_name(电影/电视剧/movie/tv)映射到 interests 路径段。"""
    text = str(type_name or "").strip().lower()
    if "电视" in text or "剧集" in text or text == "tv":
        return "tv"
    return "movie"


def build_douban_comments_loader(
    client: DoubanCommentsClient, douban_id: str | int, kind: str = "movie"
) -> CommentsLoader:
    """把豆瓣 interests 适配成评论对话框的 CommentsLoader 协议(只读 main 分页)。"""

    def loader(request: dict[str, object]) -> dict[str, object]:
        raw_next = str(request.get("next") or "").strip()
        start = int(raw_next) if raw_next.isdigit() else 0
        payload = client.list_interests(douban_id, kind, start=start, count=_PAGE_SIZE)
        return _to_comments_payload(payload, start)

    return loader


def _to_comments_payload(payload: dict[str, Any], start: int) -> dict[str, object]:
    interests = [entry for entry in payload.get("interests") or [] if isinstance(entry, dict)]
    total = int(payload.get("total") or 0)
    comments: list[dict[str, object]] = []
    for entry in interests:
        user = entry.get("user") or {}
        rating = entry.get("rating") or {}
        star_count = int(rating.get("star_count") or 0)
        raw_id = entry.get("id")
        comments.append(
            {
                "rpid": "" if raw_id is None else str(raw_id).strip(),
                "uname": str(user.get("name") or "").strip(),
                "avatar": str(user.get("avatar") or "").strip(),
                "message": str(entry.get("comment") or "").strip(),
                "like": int(entry.get("vote_count") or 0),
                "time_desc": str(entry.get("create_time") or "").strip(),
                "location": str((user.get("loc") or {}).get("name") or "").strip(),
                "badge": "★" * star_count if 0 < star_count <= 5 else "",
            }
        )
    is_end = not interests or (start + len(interests)) >= total
    return {
        "count": total,
        "is_end": is_end,
        "next_offset": str(start + len(interests)),
        "comments": comments,
    }
