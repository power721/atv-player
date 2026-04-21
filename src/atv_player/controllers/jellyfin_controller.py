from __future__ import annotations

import json
from collections.abc import Callable

from atv_player.controllers.browse_controller import _map_vod_item
from atv_player.controllers.douban_controller import _map_category, _map_item
from atv_player.controllers.telegram_search_controller import _parse_playlist
from atv_player.models import DoubanCategory, HistoryRecord, OpenPlayerRequest, PlayItem, VodItem


class JellyfinController:
    _PAGE_SIZE = 30

    def __init__(
        self,
        api_client,
        playback_history_loader: Callable[[str], HistoryRecord | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self._api_client = api_client
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver

    def load_categories(self) -> list[DoubanCategory]:
        payload = self._api_client.list_jellyfin_categories()
        categories = [_map_category(item) for item in payload.get("class", [])]
        categories = [category for category in categories if category.type_id != "0"]
        return [DoubanCategory(type_id="0", type_name="推荐"), *categories]

    def _decorate_card_subtitle(self, item: VodItem) -> VodItem:
        subtitle_parts = [item.vod_year.strip(), item.vod_remarks.strip()]
        item.vod_remarks = " - ".join(part for part in subtitle_parts if part)
        return item

    def _map_jellyfin_items(self, payload: dict) -> list[VodItem]:
        return [self._decorate_card_subtitle(_map_item(item)) for item in payload.get("list", [])]

    def load_items(self, category_id: str, page: int) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_jellyfin_items(category_id, page=page)
        items = self._map_jellyfin_items(payload)
        total_raw = payload.get("total")
        if total_raw is not None:
            total = int(total_raw)
        else:
            pagecount = int(payload.get("pagecount") or 0)
            total = pagecount * self._PAGE_SIZE
        return items, total

    def search_items(self, keyword: str, page: int) -> tuple[list[VodItem], int]:
        payload = self._api_client.search_jellyfin_items(keyword, page=page)
        items = self._map_jellyfin_items(payload)
        total_raw = payload.get("total")
        if total_raw is not None:
            total = int(total_raw)
        else:
            pagecount = int(payload.get("pagecount") or 0)
            total = pagecount * self._PAGE_SIZE
        return items, total

    def load_folder_items(self, vod_id: str) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_jellyfin_items(vod_id, page=1)
        items = self._map_jellyfin_items(payload)
        total_raw = payload.get("total")
        total = int(total_raw) if total_raw is not None else len(items)
        return items, total

    def resolve_playlist_item(self, item: PlayItem) -> VodItem | None:
        if not item.vod_id:
            return None
        try:
            payload = self._api_client.get_jellyfin_detail(item.vod_id)
            return _map_vod_item(payload["list"][0])
        except (KeyError, IndexError):
            return None

    def load_playback_item(self, item: PlayItem) -> None:
        if not item.vod_id:
            raise ValueError("缺少 Jellyfin 播放 ID")
        payload = self._api_client.get_jellyfin_playback_source(item.vod_id)
        raw_url = payload.get("url")
        if isinstance(raw_url, list):
            candidates = [str(value or "").strip() for index, value in enumerate(raw_url) if index % 2 == 1]
            play_url = next((candidate for candidate in candidates if candidate), "")
        else:
            play_url = str(raw_url or "")
        if not play_url:
            raise ValueError(f"没有可用的播放地址: {item.title}")
        headers = payload.get("header") or {}
        if isinstance(headers, str):
            try:
                parsed_headers = json.loads(headers)
            except json.JSONDecodeError:
                parsed_headers = {}
            headers = parsed_headers if isinstance(parsed_headers, dict) else {}
        item.url = play_url
        item.headers = {str(key): str(value) for key, value in headers.items()}

    def report_playback_progress(self, item: PlayItem, position_ms: int, paused: bool) -> None:
        return

    def stop_playback(self, item: PlayItem) -> None:
        if not item.vod_id:
            return
        self._api_client.stop_jellyfin_playback(item.vod_id)

    def _build_playlist(self, detail: VodItem) -> list[PlayItem]:
        playlist = _parse_playlist(detail.vod_play_url)
        if len(playlist) == 1 and not playlist[0].vod_id:
            playlist[0].title = detail.vod_name or playlist[0].title
            playlist[0].vod_id = detail.vod_play_url.strip() or detail.vod_id
        if not playlist and detail.vod_play_url:
            playlist = [
                PlayItem(
                    title=detail.vod_name or detail.vod_play_url,
                    url="",
                    vod_id=detail.vod_play_url.strip() or detail.vod_id,
                )
            ]
        return playlist

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        payload = self._api_client.get_jellyfin_detail(vod_id)
        detail = _map_vod_item(payload["list"][0])
        playlist = self._build_playlist(detail)
        if not playlist and detail.items:
            playlist = list(detail.items)
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
            source_kind="jellyfin",
            source_mode="detail",
            source_vod_id=detail.vod_id,
            use_local_history=False,
            detail_resolver=self.resolve_playlist_item,
            playback_loader=self.load_playback_item,
            playback_progress_reporter=self.report_playback_progress,
            playback_stopper=self.stop_playback,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )
