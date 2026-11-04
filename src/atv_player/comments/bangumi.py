from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from atv_player.network_proxy import ProxyDecider, build_httpx_kwargs_for_url

# 官方 v0 API 没有条目评论端点,吐槽走 beta 后端 next.bgm.tv(p1)。
_NEXT_COMMENTS_URL = "https://next.bgm.tv/p1/subjects/{subject_id}/comments"
_SEARCH_URL = "https://api.bgm.tv/v0/search/subjects"
# Bangumi 要求 UA 自报家门(应用名/版本 + 联系方式)
_USER_AGENT = "ATVPlayer/1.0 (https://github.com/har01d/atv-player)"
_PAGE_SIZE = 20

CommentsLoader = Callable[[dict[str, object]], dict[str, object]]


class BangumiCommentsClient:
    """Bangumi 条目吐槽只读客户端(next.bgm.tv p1 + 官方 v0 搜索)。"""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        proxy_decider: ProxyDecider | None = None,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        client_kwargs: dict[str, Any] = dict(transport=transport, timeout=10.0)
        client_kwargs.update(build_httpx_kwargs_for_url(proxy_decider, _NEXT_COMMENTS_URL))
        client_kwargs.update(build_httpx_kwargs_for_url(proxy_decider, _SEARCH_URL))
        self._client = client_factory(**client_kwargs)

    def list_subject_comments(
        self, subject_id: str | int, limit: int = _PAGE_SIZE, offset: int = 0
    ) -> dict[str, Any]:
        subject = str(subject_id).strip()
        if not subject.isdigit():
            raise ValueError("Bangumi 条目 ID 为空")
        response = self._client.get(
            _NEXT_COMMENTS_URL.format(subject_id=subject),
            params={"limit": max(1, int(limit)), "offset": max(0, int(offset))},
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Bangumi 吐槽响应格式异常")
        return payload

    def search_subjects(self, keyword: str) -> list[dict[str, Any]]:
        """标题搜索动画/三次元条目(v0 搜索,type 2=动画 6=三次元)。"""
        text = str(keyword or "").strip()
        if not text:
            return []
        response = self._client.post(
            _SEARCH_URL,
            json={"keyword": text, "filter": {"type": [2, 6]}},
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        return [entry for entry in data or [] if isinstance(entry, dict)]


def build_bangumi_comments_loader(
    client: BangumiCommentsClient, subject_id: str | int
) -> CommentsLoader:
    """把 Bangumi 条目吐槽适配成评论对话框的 CommentsLoader 协议(只读 main 分页)。"""

    def loader(request: dict[str, object]) -> dict[str, object]:
        raw_next = str(request.get("next") or "").strip()
        offset = int(raw_next) if raw_next.isdigit() else 0
        payload = client.list_subject_comments(subject_id, limit=_PAGE_SIZE, offset=offset)
        return _to_comments_payload(payload, offset)

    return loader


def _to_comments_payload(payload: dict[str, Any], offset: int) -> dict[str, object]:
    data = [entry for entry in payload.get("data") or [] if isinstance(entry, dict)]
    total = int(payload.get("total") or 0)
    comments: list[dict[str, object]] = []
    for entry in data:
        user = entry.get("user") or {}
        avatar = user.get("avatar") or {}
        rate = int(entry.get("rate") or 0)
        raw_id = entry.get("id")
        comments.append(
            {
                "rpid": "" if raw_id is None else str(raw_id).strip(),
                "uname": str(user.get("nickname") or user.get("username") or "").strip(),
                "avatar": str(avatar.get("medium") or avatar.get("large") or "").strip(),
                "message": str(entry.get("comment") or "").strip(),
                "ctime": int(entry.get("updatedAt") or 0),
                "badge": f"{rate}分" if 0 < rate <= 10 else "",
            }
        )
    is_end = not data or (offset + len(data)) >= total
    return {
        "count": total,
        "is_end": is_end,
        "next_offset": str(offset + len(data)),
        "comments": comments,
    }
