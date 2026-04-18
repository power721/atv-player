from __future__ import annotations

import json
from collections.abc import Callable
from collections.abc import Mapping

from atv_player.api import ApiError
from atv_player.controllers.browse_controller import _map_vod_item
from atv_player.controllers.douban_controller import _map_category, _map_item
from atv_player.models import DoubanCategory, OpenPlayerRequest, PlayItem, VodItem


def _looks_like_media_url(value: str) -> bool:
    candidate = value.strip().lower()
    return candidate.startswith(("http://", "https://", "rtmp://", "rtsp://")) or any(
        ext in candidate for ext in (".m3u8", ".mp4", ".flv")
    )


def _normalize_headers(raw_headers) -> dict[str, str]:
    if not raw_headers:
        return {}
    if isinstance(raw_headers, Mapping):
        return {str(key): str(value) for key, value in raw_headers.items()}
    if isinstance(raw_headers, str):
        try:
            parsed = json.loads(raw_headers)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return {str(key): str(value) for key, value in parsed.items()}
        return {}
    return {}


class SpiderPluginController:
    def __init__(
        self,
        spider,
        plugin_name: str,
        search_enabled: bool,
        playback_history_loader: Callable[[str], object | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self._spider = spider
        self._plugin_name = plugin_name
        self.supports_search = search_enabled
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver
        self._home_loaded = False
        self._home_categories: list[DoubanCategory] = []
        self._home_items: list[VodItem] = []

    def _map_items(self, payload: dict) -> list[VodItem]:
        return [_map_item(item) for item in payload.get("list", [])]

    def _ensure_home_loaded(self) -> None:
        if self._home_loaded:
            return
        try:
            payload = self._spider.homeContent(False) or {}
        except Exception as exc:
            raise ApiError(str(exc)) from exc
        categories = [_map_category(item) for item in payload.get("class", [])]
        items = self._map_items(payload)
        if items:
            categories = [DoubanCategory(type_id="home", type_name="推荐"), *categories]
        self._home_categories = categories
        self._home_items = items
        self._home_loaded = True

    def load_categories(self) -> list[DoubanCategory]:
        self._ensure_home_loaded()
        return list(self._home_categories)

    def load_items(self, category_id: str, page: int) -> tuple[list[VodItem], int]:
        self._ensure_home_loaded()
        if category_id == "home":
            return list(self._home_items), len(self._home_items)
        try:
            payload = self._spider.categoryContent(category_id, str(page), False, {}) or {}
        except Exception as exc:
            raise ApiError(str(exc)) from exc
        items = self._map_items(payload)
        total = int(payload.get("total") or 0)
        if total <= 0:
            total = len(items)
        return items, total

    def search_items(self, keyword: str, page: int) -> tuple[list[VodItem], int]:
        if not self.supports_search:
            raise ApiError("当前插件不支持搜索")
        try:
            payload = self._spider.searchContent(keyword, False, str(page)) or {}
        except Exception as exc:
            raise ApiError(str(exc)) from exc
        items = self._map_items(payload)
        total = int(payload.get("total") or len(items))
        return items, total

    def _build_playlist(self, detail: VodItem) -> list[PlayItem]:
        routes = [item.strip() for item in (detail.vod_play_from or "").split("$$$")]
        groups = (detail.vod_play_url or "").split("$$$")
        playlist: list[PlayItem] = []
        for group_index, group in enumerate(groups):
            route = routes[group_index] if group_index < len(routes) else ""
            for raw_chunk in group.split("#"):
                chunk = raw_chunk.strip()
                if not chunk:
                    continue
                title, separator, value = chunk.partition("$")
                if not separator:
                    title = chunk
                    value = chunk
                display = title.strip() or value.strip() or f"选集 {len(playlist) + 1}"
                if route:
                    display = f"{route} | {display}"
                clean_value = value.strip()
                playlist.append(
                    PlayItem(
                        title=display,
                        url=clean_value if _looks_like_media_url(clean_value) else "",
                        vod_id="" if _looks_like_media_url(clean_value) else clean_value,
                        index=len(playlist),
                        play_source=route,
                    )
                )
        return playlist

    def _resolve_play_item(self, item: PlayItem) -> None:
        if item.url or not item.vod_id:
            return
        try:
            payload = self._spider.playerContent(item.play_source, item.vod_id, []) or {}
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        url = str(payload.get("url") or "").strip()
        if not _looks_like_media_url(url):
            raise ValueError("插件未返回可播放地址")
        item.url = url
        item.headers = _normalize_headers(payload.get("header"))

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        try:
            payload = self._spider.detailContent([vod_id]) or {}
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        try:
            detail = _map_vod_item(payload["list"][0])
        except (KeyError, IndexError) as exc:
            raise ValueError(f"没有可播放的项目: {vod_id}") from exc
        playlist = self._build_playlist(detail)
        if not playlist:
            raise ValueError(f"没有可播放的项目: {detail.vod_name}")
        history_loader = None
        history_saver = None
        if self._playback_history_loader is not None:
            history_loader = lambda source_vod_id=detail.vod_id: self._playback_history_loader(source_vod_id)
        if self._playback_history_saver is not None:
            history_saver = lambda payload, source_vod_id=detail.vod_id: self._playback_history_saver(
                source_vod_id,
                payload,
            )
        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            clicked_index=0,
            source_kind="plugin",
            source_mode="detail",
            source_vod_id=detail.vod_id,
            use_local_history=False,
            playback_loader=self._resolve_play_item,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )
