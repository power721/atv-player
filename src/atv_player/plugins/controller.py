from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from collections.abc import Mapping
from urllib.parse import urlparse

from atv_player.api import ApiError
from atv_player.danmaku.cache import load_cached_danmaku_xml, save_cached_danmaku_xml
from atv_player.danmaku.utils import infer_playlist_episode_number
from atv_player.controllers.browse_controller import _map_vod_item
from atv_player.controllers.douban_controller import _map_category, _map_item
from atv_player.controllers.telegram_search_controller import build_detail_playlist
from atv_player.models import DoubanCategory, OpenPlayerRequest, PlayItem, PlaybackLoadResult, VodItem
from atv_player.player.resume import resolve_resume_index


logger = logging.getLogger(__name__)


def _looks_like_media_url(value: str) -> bool:
    candidate = value.strip().lower()
    if candidate.endswith(".html"):
        return False
    if candidate.startswith(("http://", "https://", "rtmp://", "rtsp://")):
        return True
    return any(candidate.endswith(ext) or f"{ext}?" in candidate for ext in (".m3u8", ".mkv", ".mp4", ".flv"))


def _has_implicit_numeric_title(value: str) -> bool:
    return re.fullmatch(r"\s*0*\d{1,4}\s*", value or "") is not None


def _looks_like_calendar_episode_title(value: str) -> bool:
    candidate = (value or "").strip()
    if not candidate:
        return False
    return (
        re.match(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:\D|$)", candidate) is not None
        or re.match(r"^(?:19|20)\d{2}[-/.年](?:0?[1-9]|1[0-2])[-/.月](?:0?[1-9]|[12]\d|3[01])(?:日|\D|$)", candidate)
        is not None
    )


def _is_short_bare_numeric_playlist(item: PlayItem, playlist: list[PlayItem] | None = None) -> bool:
    if item.danmaku_title_only:
        return True
    if not _has_implicit_numeric_title(item.title) or not playlist:
        return False
    if len(playlist) < 2 or len(playlist) > 4:
        return False
    return all(_has_implicit_numeric_title(candidate.title) for candidate in playlist)


def _extract_episode_label(item: PlayItem, playlist: list[PlayItem] | None = None) -> str:
    if _looks_like_calendar_episode_title(item.title):
        return item.title.strip()
    episode_number = infer_playlist_episode_number(item, playlist)
    if episode_number is None:
        return ""
    if _is_short_bare_numeric_playlist(item, playlist):
        return ""
    if _has_implicit_numeric_title(item.title):
        return str(episode_number)
    return f"{episode_number}集"


def _mark_short_bare_numeric_playlist(playlist: list[PlayItem]) -> list[PlayItem]:
    if len(playlist) < 2 or len(playlist) > 4:
        return playlist
    if not all(_has_implicit_numeric_title(item.title) for item in playlist):
        return playlist
    for item in playlist:
        item.danmaku_title_only = True
    return playlist


def _build_danmaku_search_name(item: PlayItem, playlist: list[PlayItem] | None = None) -> str:
    media_title = item.media_title.strip()
    if not media_title:
        return item.title.strip()
    episode_label = _extract_episode_label(item, playlist)
    return " ".join(part for part in (media_title, episode_label) if part).strip()


def _should_prefetch_danmaku(item: PlayItem, playlist: list[PlayItem] | None = None) -> bool:
    return bool(_extract_episode_label(item, playlist))


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


_SUPPORTED_DRIVE_DOMAINS = (
    "alipan.com",
    "aliyundrive.com",
    "mypikpak.com",
    "xunlei.com",
    "123pan.com",
    "123pan.cn",
    "123684.com",
    "123865.com",
    "123912.com",
    "123592.com",
    "quark.cn",
    "139.com",
    "uc.cn",
    "115.com",
    "115cdn.com",
    "anxia.com",
    "189.cn",
    "baidu.com",
)

_DRIVE_PROVIDER_LABELS = {
    "alipan.com": "阿里",
    "aliyundrive.com": "阿里",
    "mypikpak.com": "PikPak",
    "xunlei.com": "迅雷",
    "123pan.com": "123云盘",
    "123pan.cn": "123云盘",
    "123684.com": "123云盘",
    "123865.com": "123云盘",
    "123912.com": "123云盘",
    "123592.com": "123云盘",
    "quark.cn": "夸克",
    "139.com": "移动云盘",
    "uc.cn": "UC",
    "115.com": "115",
    "115cdn.com": "115",
    "anxia.com": "115",
    "189.cn": "天翼",
    "baidu.com": "百度",
}


def _looks_like_drive_share_link(value: str) -> bool:
    candidate = value.strip()
    url = candidate.lower()
    if not url.startswith(("http://", "https://")):
        return False
    if url.endswith((".m3u8", ".mkv", ".mp4", ".flv")):
        return False
    hostname = (urlparse(candidate).hostname or "").lower()
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in _SUPPORTED_DRIVE_DOMAINS)


def _detect_drive_provider_label(value: str) -> str:
    candidate = value.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return ""
    hostname = (urlparse(candidate).hostname or "").lower()
    for domain, label in _DRIVE_PROVIDER_LABELS.items():
        if hostname == domain or hostname.endswith(f".{domain}"):
            return label
    return ""


def _format_drive_route_label(route: str, provider: str) -> str:
    normalized_route = route.strip()
    if not provider or provider in normalized_route:
        return normalized_route
    return f"{normalized_route}({provider})"


class SpiderPluginController:
    def __init__(
        self,
        spider,
        plugin_name: str,
        search_enabled: bool,
        drive_detail_loader: Callable[[str], dict] | None = None,
        playback_history_loader: Callable[[str], object | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
        playback_parser_service=None,
        preferred_parse_key_loader: Callable[[], str] | None = None,
        danmaku_service=None,
    ) -> None:
        self._spider = spider
        self._plugin_name = plugin_name
        self.supports_search = search_enabled
        self._drive_detail_loader = drive_detail_loader
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver
        self._playback_parser_service = playback_parser_service
        self._preferred_parse_key_loader = preferred_parse_key_loader
        self._danmaku_service = danmaku_service
        self._danmaku_enabled = bool(getattr(self._spider, "danmaku", lambda: False)())
        self._danmaku_lock = threading.Lock()
        self._pending_danmaku_item_ids: set[int] = set()
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
            logger.exception("Spider plugin home load failed plugin=%s", self._plugin_name)
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
            logger.exception(
                "Spider plugin category load failed plugin=%s category_id=%s page=%s",
                self._plugin_name,
                category_id,
                page,
            )
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
            logger.exception(
                "Spider plugin search failed plugin=%s keyword=%s page=%s",
                self._plugin_name,
                keyword,
                page,
            )
            raise ApiError(str(exc)) from exc
        items = self._map_items(payload)
        total = int(payload.get("total") or len(items))
        return items, total

    def _route_name(self, routes: list[str], group_index: int) -> str:
        route = routes[group_index] if group_index < len(routes) else ""
        route = route.strip()
        return route or f"线路 {group_index + 1}"

    def _build_playlist(self, detail: VodItem) -> list[list[PlayItem]]:
        routes = [item.strip() for item in (detail.vod_play_from or "").split("$$$")]
        groups = (detail.vod_play_url or "").split("$$$")
        playlists: list[list[PlayItem]] = []
        for group_index, group in enumerate(groups):
            route = self._route_name(routes, group_index)
            route_label = route
            playlist: list[PlayItem] = []
            for raw_chunk in group.split("#"):
                chunk = raw_chunk.strip()
                if not chunk:
                    continue
                title, separator, value = chunk.partition("$")
                if not separator:
                    title = chunk
                    value = chunk
                clean_value = value.strip()
                is_drive_link = _looks_like_drive_share_link(clean_value)
                is_media_url = _looks_like_media_url(clean_value) and not is_drive_link
                if is_drive_link:
                    provider = _detect_drive_provider_label(clean_value)
                    if provider:
                        route_label = _format_drive_route_label(route, provider)
                playlist.append(
                    PlayItem(
                        title=title.strip() or clean_value or f"选集 {len(playlist) + 1}",
                        url=clean_value if is_media_url else "",
                        media_title=detail.vod_name,
                        path=detail.vod_id if is_drive_link else "",
                        vod_id="" if is_media_url else clean_value,
                        index=len(playlist),
                        play_source=route_label,
                    )
                )
            if playlist:
                playlists.append(_mark_short_bare_numeric_playlist(playlist))
        return playlists

    def _build_drive_replacement_playlist(self, detail: VodItem, play_source: str, media_title: str = "") -> list[PlayItem]:
        resolved_media_title = media_title.strip() or detail.vod_name
        if detail.items:
            return _mark_short_bare_numeric_playlist([
                PlayItem(
                    title=item.title,
                    url=item.url,
                    media_title=resolved_media_title,
                    path=item.path,
                    index=index,
                    size=item.size,
                    vod_id=item.vod_id,
                    headers=dict(item.headers),
                    play_source=play_source,
                )
                for index, item in enumerate(detail.items)
                if item.url
            ])
        playlist = build_detail_playlist(detail)
        return _mark_short_bare_numeric_playlist([
            PlayItem(
                title=item.title,
                url=item.url,
                media_title=resolved_media_title,
                path=item.path,
                index=index,
                size=item.size,
                vod_id=item.vod_id,
                headers=dict(item.headers),
                play_source=play_source,
            )
            for index, item in enumerate(playlist)
            if item.url and not _looks_like_drive_share_link(item.url)
        ])

    def _resolve_danmaku_sync(self, item: PlayItem, url: str, playlist: list[PlayItem] | None = None) -> None:
        if not self._danmaku_enabled or self._danmaku_service is None:
            return
        search_name = _build_danmaku_search_name(item, playlist)
        if not search_name:
            return
        reg_src = str(item.vod_id or url or "").strip()
        cached_xml = load_cached_danmaku_xml(search_name, reg_src)
        if cached_xml:
            item.danmaku_xml = cached_xml
            logger.info(
                "Spider plugin loaded cached danmaku plugin=%s source=%s",
                self._plugin_name,
                item.vod_id,
            )
            return
        try:
            candidates = self._danmaku_service.search_danmu(search_name, reg_src)
        except Exception as exc:
            logger.warning(
                "Spider plugin danmaku search failed plugin=%s source=%s error=%s",
                self._plugin_name,
                item.vod_id,
                exc,
            )
            return
        if not candidates:
            return
        for candidate in candidates:
            try:
                item.danmaku_xml = self._danmaku_service.resolve_danmu(candidate.url)
                save_cached_danmaku_xml(search_name, reg_src, item.danmaku_xml)
                logger.info(
                    "Spider plugin resolved danmaku plugin=%s source=%s candidate=%s",
                    self._plugin_name,
                    item.vod_id,
                    candidate.url,
                )
                return
            except Exception as exc:
                logger.warning(
                    "Spider plugin danmaku candidate failed plugin=%s source=%s candidate=%s error=%s",
                    self._plugin_name,
                    item.vod_id,
                    candidate.url,
                    exc,
                )

    def _maybe_resolve_danmaku(self, item: PlayItem, url: str, playlist: list[PlayItem] | None = None) -> None:
        if not self._danmaku_enabled or self._danmaku_service is None:
            return
        if item.danmaku_xml or item.danmaku_pending:
            return
        item_id = id(item)
        with self._danmaku_lock:
            if item_id in self._pending_danmaku_item_ids:
                return
            self._pending_danmaku_item_ids.add(item_id)
        item.danmaku_pending = True

        def run() -> None:
            try:
                self._resolve_danmaku_sync(item, url, playlist)
            finally:
                item.danmaku_pending = False
                with self._danmaku_lock:
                    self._pending_danmaku_item_ids.discard(item_id)

        threading.Thread(target=run, daemon=True).start()

    def _resolve_replacement_start_index(self, vod_id: str, replacement: list[PlayItem]) -> int:
        if self._playback_history_loader is None or not replacement:
            return 0
        history = self._playback_history_loader(vod_id)
        return resolve_resume_index(history, replacement, 0)

    def _resolve_play_item(self, item: PlayItem) -> PlaybackLoadResult | None:
        if item.url:
            if not item.danmaku_xml:
                self._maybe_resolve_danmaku(item, item.url)
            return
        if not item.vod_id:
            return
        if _looks_like_drive_share_link(item.vod_id):
            if self._drive_detail_loader is None:
                raise ValueError("当前插件未配置网盘解析")
            try:
                payload = self._drive_detail_loader(item.vod_id)
                detail = _map_vod_item(payload["list"][0])
            except (KeyError, IndexError) as exc:
                logger.exception(
                    "Spider plugin drive detail failed plugin=%s source=%s",
                    self._plugin_name,
                    item.vod_id,
                )
                raise ValueError(f"没有可播放的项目: {item.title or item.vod_id}") from exc
            replacement = self._build_drive_replacement_playlist(detail, item.play_source, media_title=item.media_title)
            if not replacement:
                raise ValueError(f"没有可播放的项目: {detail.vod_name or item.title}")
            replacement_start_index = self._resolve_replacement_start_index(item.path or detail.vod_id, replacement)
            replacement_item = replacement[replacement_start_index]
            if _should_prefetch_danmaku(replacement_item, replacement):
                self._maybe_resolve_danmaku(replacement_item, item.vod_id, replacement)
            logger.info(
                "Spider plugin resolved drive playlist plugin=%s source=%s items=%s",
                self._plugin_name,
                item.vod_id,
                len(replacement),
            )
            return PlaybackLoadResult(
                replacement_playlist=replacement,
                replacement_start_index=replacement_start_index,
            )
        try:
            payload = self._spider.playerContent(item.play_source, item.vod_id, []) or {}
        except Exception as exc:
            logger.exception(
                "Spider plugin playback resolve failed plugin=%s source=%s",
                self._plugin_name,
                item.vod_id,
            )
            raise ValueError(str(exc)) from exc
        parse_required = int(payload.get("parse") or 0) == 1
        url = str(payload.get("url") or "").strip()
        if _looks_like_drive_share_link(url):
            if self._drive_detail_loader is None:
                raise ValueError("当前插件未配置网盘解析")
            try:
                payload = self._drive_detail_loader(url)
                detail = _map_vod_item(payload["list"][0])
            except (KeyError, IndexError) as exc:
                logger.exception(
                    "Spider plugin drive detail failed plugin=%s source=%s",
                    self._plugin_name,
                    item.vod_id,
                )
                raise ValueError(f"没有可播放的项目: {item.title or item.vod_id}") from exc
            replacement = self._build_drive_replacement_playlist(detail, item.play_source, media_title=item.media_title)
            if not replacement:
                raise ValueError(f"没有可播放的项目: {detail.vod_name or item.title}")
            replacement_start_index = self._resolve_replacement_start_index(item.path or detail.vod_id, replacement)
            replacement_item = replacement[replacement_start_index]
            if _should_prefetch_danmaku(replacement_item, replacement):
                self._maybe_resolve_danmaku(replacement_item, url, replacement)
            logger.info(
                "Spider plugin resolved drive playlist plugin=%s source=%s items=%s",
                self._plugin_name,
                item.vod_id,
                len(replacement),
            )
            return PlaybackLoadResult(
                replacement_playlist=replacement,
                replacement_start_index=replacement_start_index,
            )
        if parse_required:
            if self._playback_parser_service is None:
                raise ValueError("当前插件未配置内置解析")
            result = self._playback_parser_service.resolve(
                item.play_source,
                url,
                preferred_key="" if self._preferred_parse_key_loader is None else self._preferred_parse_key_loader(),
            )
            item.url = result.url
            item.headers = dict(result.headers)
            self._maybe_resolve_danmaku(item, url)
            logger.info(
                "Spider plugin resolved parse playback plugin=%s source=%s parser=%s",
                self._plugin_name,
                item.vod_id,
                result.parser_key,
            )
            return None
        if not _looks_like_media_url(url):
            raise ValueError("插件未返回可播放地址")
        item.url = url
        item.headers = _normalize_headers(payload.get("header"))
        self._maybe_resolve_danmaku(item, url)
        logger.info(
            "Spider plugin resolved playback url plugin=%s source=%s play_source=%s",
            self._plugin_name,
            item.vod_id,
            item.play_source,
        )
        return None

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        try:
            payload = self._spider.detailContent([vod_id]) or {}
        except Exception as exc:
            logger.exception("Spider plugin detail load failed plugin=%s vod_id=%s", self._plugin_name, vod_id)
            raise ValueError(str(exc)) from exc
        try:
            detail = _map_vod_item(payload["list"][0])
        except (KeyError, IndexError) as exc:
            raise ValueError(f"没有可播放的项目: {vod_id}") from exc
        playlists = self._build_playlist(detail)
        if not playlists:
            raise ValueError(f"没有可播放的项目: {detail.vod_name}")
        logger.info(
            "Spider plugin build request plugin=%s vod_id=%s routes=%s",
            self._plugin_name,
            detail.vod_id,
            len(playlists),
        )
        playlist = playlists[0]
        source_vod_id = vod_id or detail.vod_id
        history_loader = None
        history_saver = None
        if self._playback_history_loader is not None:
            history_loader = lambda source_vod_id=source_vod_id: self._playback_history_loader(source_vod_id)
        if self._playback_history_saver is not None:
            history_saver = lambda payload, source_vod_id=source_vod_id: self._playback_history_saver(
                source_vod_id,
                payload,
            )
        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            playlists=playlists,
            playlist_index=0,
            clicked_index=0,
            source_kind="plugin",
            source_mode="detail",
            source_vod_id=source_vod_id,
            use_local_history=False,
            playback_loader=self._resolve_play_item,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )
