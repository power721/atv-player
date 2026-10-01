from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

import httpx

from atv_player.controllers.browse_controller import _map_vod_item
from atv_player.controllers.douban_controller import _map_categories, _map_item
from atv_player.controllers.pagination import page_count_from_payload
from atv_player.controllers.telegram_search_controller import _parse_playlist
from atv_player.models import (
    DoubanCategory,
    ExternalSubtitleOption,
    HistoryRecord,
    OpenPlayerRequest,
    PlayChapter,
    PlayItem,
    PlaybackDetailAction,
    PlaybackDetailField,
    PlaybackDetailFieldAction,
    PlaybackDetailValuePart,
    VodItem,
)

_JAVA_MAP_HEADER_ENTRY_RE = re.compile(r"(?:^|,\s*)([A-Za-z0-9-]+)=(.*?)(?=,\s*[A-Za-z0-9-]+=|$)")
_BILIBILI_DANMAKU_URL_RE = re.compile(r"^https?://comment\.bilibili\.com/\d+\.xml(?:\?.*)?$", re.IGNORECASE)
_BILIBILI_BVID_RE = re.compile(r"^BV[0-9A-Za-z]+$")
_BILIBILI_SS_ID_RE = re.compile(r"^ss(\d+)$", re.IGNORECASE)
_BILIBILI_SEASON_ID_RE = re.compile(r"^season\$(\d+)$", re.IGNORECASE)
_BILIBILI_DETAIL_FIELD_SPECS = (
    ("coin", "投币"),
    ("like", "点赞"),
    ("favorite", "收藏"),
    ("reply", "回复"),
    ("danmaku", "弹幕"),
)

logger = logging.getLogger(__name__)


def _parse_bilibili_headers(headers: object) -> dict[str, str]:
    if isinstance(headers, dict):
        return {str(key): str(value) for key, value in headers.items()}
    if not isinstance(headers, str):
        return {}
    text = headers.strip()
    if not text:
        return {}
    try:
        parsed_headers = json.loads(text)
    except json.JSONDecodeError:
        parsed_headers = None
    if isinstance(parsed_headers, dict):
        return {str(key): str(value) for key, value in parsed_headers.items()}
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1].strip()
    parsed: dict[str, str] = {}
    for match in _JAVA_MAP_HEADER_ENTRY_RE.finditer(text):
        key = match.group(1).strip()
        value = match.group(2).strip()
        if key:
            parsed[key] = value
    return parsed


def _parse_bilibili_chapters(raw: object) -> list[PlayChapter]:
    if not isinstance(raw, list):
        return []
    chapters: list[PlayChapter] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        raw_from = entry.get("from")
        if isinstance(raw_from, bool) or raw_from is None:
            continue
        try:
            start = float(raw_from)
        except (TypeError, ValueError, OverflowError):
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        try:
            end = float(entry.get("to") or 0)
        except (TypeError, ValueError, OverflowError):
            end = 0.0
        chapters.append(
            PlayChapter(title=title, start_seconds=max(0.0, start), end_seconds=max(0.0, end))
        )
    chapters.sort(key=lambda chapter: chapter.start_seconds)
    return chapters


def _map_detail_actions(payload: object) -> list[PlaybackDetailAction]:
    if not isinstance(payload, list):
        return []
    actions: list[PlaybackDetailAction] = []
    for raw_action in payload:
        if not isinstance(raw_action, dict):
            continue
        action_id = str(raw_action.get("id") or "").strip()
        label = str(raw_action.get("label") or "").strip()
        if not action_id or not label:
            continue
        visible = bool(raw_action.get("visible", True))
        if not visible:
            continue
        actions.append(
            PlaybackDetailAction(
                id=action_id,
                label=label,
                active=bool(raw_action.get("active")),
                enabled=bool(raw_action.get("enabled", True)),
                tooltip=str(raw_action.get("tooltip") or "").strip(),
                icon=str(raw_action.get("icon") or "").strip(),
            )
        )
    return actions


def _map_bilibili_detail_fields(payload: object, vod_id: object | None = None) -> list[PlaybackDetailField]:
    if not isinstance(payload, dict):
        return []
    normalized_vod_id = str(vod_id or "").strip()
    comments_bvid = normalized_vod_id if _BILIBILI_BVID_RE.match(normalized_vod_id) else ""
    fields: list[PlaybackDetailField] = []
    for key, label in _BILIBILI_DETAIL_FIELD_SPECS:
        raw_value = payload.get(key)
        if raw_value is None:
            continue
        value = _format_bilibili_stat_value(raw_value)
        if not value:
            continue
        if key == "reply" and comments_bvid:
            # BV 视频的评论总数可点开评论对话框;ss 番剧评论区类型不同,保持纯文本
            fields.append(
                PlaybackDetailField(
                    label=label,
                    value_parts=[
                        PlaybackDetailValuePart(
                            label=value,
                            action=PlaybackDetailFieldAction(type="comments", value=comments_bvid, target="bilibili"),
                        )
                    ],
                )
            )
            continue
        fields.append(PlaybackDetailField(label=label, value=value))
    return fields


def _bilibili_link_field(label: str, display_value: str, action_value: str) -> PlaybackDetailField:
    return PlaybackDetailField(
        label=label,
        value_parts=[
            PlaybackDetailValuePart(
                label=display_value,
                action=PlaybackDetailFieldAction(type="link", value=action_value, target="bilibili"),
            )
        ],
    )


def _map_bilibili_web_id_fields(vod_id: object, ext_payload: object) -> list[PlaybackDetailField]:
    fields: list[PlaybackDetailField] = []
    normalized_vod_id = str(vod_id or "").strip()
    if _BILIBILI_BVID_RE.match(normalized_vod_id):
        fields.append(_bilibili_link_field("BVID", normalized_vod_id, normalized_vod_id))

    season_id = ""
    season_action_value = ""
    ss_match = _BILIBILI_SS_ID_RE.match(normalized_vod_id)
    if ss_match is not None:
        season_id = ss_match.group(1)
        season_action_value = normalized_vod_id
    if isinstance(ext_payload, dict):
        ids = str(ext_payload.get("ids") or "").strip()
        season_match = _BILIBILI_SEASON_ID_RE.match(ids)
        if season_match is not None and not season_id:
            season_id = season_match.group(1)
            season_action_value = f"season${season_id}"
    if season_id:
        fields.append(_bilibili_link_field("Season ID", season_id, season_action_value))
    return fields


def _format_bilibili_stat_value(raw_value: object) -> str:
    if isinstance(raw_value, bool):
        return str(raw_value)
    if isinstance(raw_value, int | float):
        if raw_value >= 10000:
            return f"{raw_value / 10000:.1f}万"
        if isinstance(raw_value, float) and raw_value.is_integer():
            return str(int(raw_value))
        return str(raw_value)
    value = str(raw_value).strip()
    if not value:
        return ""
    try:
        numeric_value = float(value)
    except ValueError:
        return value
    if numeric_value >= 10000:
        return f"{numeric_value / 10000:.1f}万"
    if numeric_value.is_integer():
        return str(int(numeric_value))
    return value


class BilibiliController:
    _PAGE_SIZE = 30
    uses_page_count_for_pagination = True

    def __init__(
        self,
        api_client,
        playback_history_loader: Callable[[str], HistoryRecord | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
        http_get: Callable[..., object] = httpx.get,
    ) -> None:
        self._api_client = api_client
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver
        self._http_get = http_get

    def _is_bilibili_danmaku_url(self, value: object) -> bool:
        return isinstance(value, str) and _BILIBILI_DANMAKU_URL_RE.match(value.strip()) is not None

    def _build_danmaku_headers(self, headers: dict[str, str]) -> dict[str, str]:
        if not headers:
            return {}
        allowed = {"referer", "user-agent", "cookie"}
        return {key: value for key, value in headers.items() if key.lower() in allowed and value}

    def _load_bilibili_danmaku(self, item: PlayItem, payload: dict[str, object]) -> None:
        danmaku_url = str(payload.get("danmaku") or "").strip()
        if not self._is_bilibili_danmaku_url(danmaku_url):
            return
        headers = self._build_danmaku_headers(item.headers)
        try:
            response = self._http_get(
                danmaku_url,
                headers=headers,
                timeout=10.0,
                follow_redirects=True,
            )
        except Exception as exc:
            item.danmaku_error = str(exc)
            logger.warning("Bilibili danmaku fetch failed vod_id=%s url=%s error=%s", item.vod_id, danmaku_url, exc)
            return
        xml_text = str(getattr(response, "text", "") or "").strip()
        if not xml_text:
            return
        item.danmaku_xml = xml_text
        item.selected_danmaku_provider = "bilibili"
        item.selected_danmaku_url = danmaku_url
        item.selected_danmaku_title = (item.media_title or item.title).strip()
        item.danmaku_error = ""

    def _normalize_bilibili_subtitle_name(self, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text in {"关闭", "关闭字幕", "off", "OFF"}:
            return ""
        return f"{text} [B站]"

    def _parse_bilibili_subtitles(self, payload: dict[str, object]) -> list[ExternalSubtitleOption]:
        raw_subs = payload.get("subs")
        if not isinstance(raw_subs, list):
            return []
        subtitles: list[ExternalSubtitleOption] = []
        for raw_sub in raw_subs:
            if not isinstance(raw_sub, dict):
                continue
            url = str(raw_sub.get("url") or "").strip()
            name = self._normalize_bilibili_subtitle_name(raw_sub.get("name"))
            if not url or not name:
                continue
            subtitles.append(
                ExternalSubtitleOption(
                    name=name,
                    lang=str(raw_sub.get("lang") or "").strip(),
                    url=url,
                    format=str(raw_sub.get("format") or "").strip(),
                    source="bilibili",
                )
            )
        return subtitles

    def load_categories(self) -> list[DoubanCategory]:
        payload = self._api_client.list_bilibili_categories()
        return _map_categories(payload)

    def load_comments(self, vod_id: str, mode: int = 3, next_offset: str = "") -> dict[str, object]:
        """评论主列表(mode 3=热门/2=最新);由播放窗口评论对话框在后台线程调用。"""
        return self._api_client.list_bilibili_comments(vod_id, mode=mode, next_offset=next_offset)

    def load_comment_replies(self, vod_id: str, root: str, page: int = 1) -> dict[str, object]:
        """楼中楼:根评论(rpid)的子回复分页。"""
        return self._api_client.list_bilibili_comment_replies(vod_id, root, page=page)

    def _decorate_card_subtitle(self, item: VodItem) -> VodItem:
        subtitle_parts = [item.vod_year.strip(), item.vod_remarks.strip()]
        item.vod_remarks = " - ".join(part for part in subtitle_parts if part)
        return item

    def _map_bilibili_items(self, payload: dict) -> list[VodItem]:
        return [self._decorate_card_subtitle(_map_item(item)) for item in payload.get("list", [])]

    def _map_bilibili_detail(self, payload: dict[str, object]) -> VodItem:
        detail = _map_vod_item(payload)
        ext_payload = payload.get("ext")
        detail.detail_fields = _map_bilibili_web_id_fields(payload.get("vod_id"), ext_payload)
        detail.detail_fields.extend(_map_bilibili_detail_fields(ext_payload, payload.get("vod_id")))
        detail.detail_style = "bilibili"
        return detail

    def load_items(
        self,
        category_id: str,
        page: int,
        filters: dict[str, str] | None = None,
    ) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_bilibili_items(category_id, page=page, filters=filters)
        items = self._map_bilibili_items(payload)
        page_count = page_count_from_payload(payload, fallback_total=len(items), page_size=self._PAGE_SIZE)
        return items, page_count

    def search_items(self, keyword: str, page: int, category_id: str = "") -> tuple[list[VodItem], int]:
        payload = self._api_client.search_bilibili_items(keyword, page=page)
        items = self._map_bilibili_items(payload)
        page_count = page_count_from_payload(payload, fallback_total=len(items), page_size=self._PAGE_SIZE)
        return items, page_count

    def load_folder_items(self, vod_id: str, page: int = 1) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_bilibili_items(vod_id, page=page)
        items = self._map_bilibili_items(payload)
        page_count = page_count_from_payload(payload, fallback_total=len(items), page_size=self._PAGE_SIZE)
        return items, page_count

    def resolve_playlist_item(self, item: PlayItem) -> VodItem | None:
        if not item.vod_id:
            return None
        try:
            payload = self._api_client.get_bilibili_detail(item.vod_id)
            return self._map_bilibili_detail(payload["list"][0])
        except (KeyError, IndexError):
            return None

    def load_playback_item(self, item: PlayItem) -> None:
        if not item.vod_id:
            raise ValueError("缺少 B站 播放 ID")
        payload = self._api_client.get_bilibili_playback_source(item.vod_id)
        raw_url = payload.get("url")
        if isinstance(raw_url, list):
            candidates = [str(value or "").strip() for index, value in enumerate(raw_url) if index % 2 == 1]
            play_url = next((candidate for candidate in candidates if candidate), "")
        else:
            play_url = str(raw_url or "")
        if not play_url:
            raise ValueError(f"没有可用的播放地址: {item.title}")
        item.url = play_url
        item.headers = _parse_bilibili_headers(payload.get("header") or {})
        item.detail_actions = _map_detail_actions(payload.get("actions"))
        item.external_subtitles = self._parse_bilibili_subtitles(payload)
        item.chapters = _parse_bilibili_chapters(payload.get("chapters"))
        self._load_bilibili_danmaku(item, payload)

    def _run_detail_action(self, vod_id: str, action_id: str) -> list[PlaybackDetailAction]:
        payload = self._api_client.run_bilibili_detail_action(vod_id, action_id) or {}
        if isinstance(payload, dict):
            return _map_detail_actions(payload.get("actions"))
        return _map_detail_actions(payload)

    def _route_name(self, routes: list[str], group_index: int) -> str:
        route = routes[group_index] if group_index < len(routes) else ""
        route = route.strip()
        return route or f"线路 {group_index + 1}"

    def _build_playlists(self, detail: VodItem) -> list[list[PlayItem]]:
        routes = [item.strip() for item in (detail.vod_play_from or "").split("$$$")]
        groups = (detail.vod_play_url or "").split("$$$")
        playlists: list[list[PlayItem]] = []
        for group_index, group in enumerate(groups):
            route = self._route_name(routes, group_index)
            playlist = _parse_playlist(group)
            for item_index, item in enumerate(playlist):
                item.index = item_index
                item.play_source = route
                item.media_title = detail.vod_name
            if len(playlist) == 1 and not playlist[0].vod_id:
                playlist[0].title = detail.vod_name or playlist[0].title
                playlist[0].vod_id = group.strip() or detail.vod_id
            if playlist:
                playlists.append(playlist)
        if not playlists and detail.vod_play_url:
            playlists = [[
                PlayItem(
                    title=detail.vod_name or detail.vod_play_url,
                    url="",
                    vod_id=detail.vod_play_url.strip() or detail.vod_id,
                    play_source=self._route_name(routes, 0),
                    media_title=detail.vod_name,
                )
            ]]
        return playlists

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        payload = self._api_client.get_bilibili_detail(vod_id)
        detail = self._map_bilibili_detail(payload["list"][0])
        playlists = self._build_playlists(detail)
        if not playlists and detail.items:
            playlists = [list(detail.items)]
        if not playlists:
            raise ValueError(f"没有可播放的项目: {detail.vod_name}")
        playlist_index = 0
        playlist = playlists[playlist_index]
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
            playlists=playlists,
            playlist_index=playlist_index,
            source_kind="bilibili",
            source_mode="detail",
            source_vod_id=detail.vod_id,
            use_local_history=False,
            detail_resolver=self.resolve_playlist_item,
            playback_loader=self.load_playback_item,
            async_playback_loader=True,
            detail_action_runner=lambda item, action_id, source_vod_id=detail.vod_id: self._run_detail_action(
                str(getattr(item, "vod_id", "") or source_vod_id),
                action_id,
            ),
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )
