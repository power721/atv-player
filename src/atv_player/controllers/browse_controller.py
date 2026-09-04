from __future__ import annotations

import math
import re
from collections.abc import Callable
from urllib.parse import urlparse

from atv_player.models import ExternalSubtitleOption, OpenPlayerRequest, PlayItem, PlaybackSource, PlaybackSourceGroup, VodItem
from atv_player.playlist_sorting import parse_size_bytes
from atv_player.share_types import get_share_type_name
from atv_player.time_utils import format_local_datetime


def build_vod_list_path(path: str) -> str:
    normalized = path or "/"
    return f"1${normalized}$1"


def _video_id_from_vod_id(vod_id: str) -> str:
    parts = str(vod_id or "").split("$")
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return str(vod_id or "")


def _safe_rating(value: object) -> float:
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return 0.0
    return rating if math.isfinite(rating) and rating > 0 else 0.0


_SUBTITLE_URL_SUFFIX_RE = re.compile(r"\.(srt|vtt|ass|ssa)(?:[?#]|$)", re.IGNORECASE)


def _external_subtitle_format_hint(entry: dict, url: str) -> str:
    """播放器按 format 决定缓存文件后缀;网盘直链路径常无扩展名,优先 ext/format。"""
    url_match = _SUBTITLE_URL_SUFFIX_RE.search(urlparse(url).path)
    if url_match is not None:
        return url_match.group(1).lower()
    ext = str(entry.get("ext") or "").strip().lstrip(".").lower()
    if ext:
        return ext
    format_name = str(entry.get("format") or "").strip().lower()
    if "subrip" in format_name or format_name == "srt":
        return "srt"
    if "vtt" in format_name:
        return "vtt"
    if "ass" in format_name:
        return "ass"
    if "ssa" in format_name:
        return "ssa"
    return ""


def _map_play_item_external_subtitles(payload: dict) -> list[ExternalSubtitleOption]:
    """解析网盘文件详情里的同目录外挂字幕(服务端 gui/web 详情下发,直链带时效)。"""
    raw_subs = payload.get("subs")
    if not isinstance(raw_subs, list):
        return []
    options: list[ExternalSubtitleOption] = []
    for entry in raw_subs:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        lang = str(entry.get("lang") or "").strip()
        label = str(entry.get("name") or "").strip() or lang or "外挂字幕"
        options.append(
            ExternalSubtitleOption(
                name=f"{label} [网盘]",
                lang=lang,
                url=url,
                format=_external_subtitle_format_hint(entry, url),
                # 复用 spider 语义:内容源自带外挂字幕,播放时自动挂载(可在字幕菜单关闭)。
                source="spider",
            )
        )
    return options


def _map_play_item(payload: dict, index: int) -> PlayItem:
    title = str(payload.get("title") or payload.get("name") or "")
    original_title = str(payload.get("name") or payload.get("title") or "")
    return PlayItem(
        title=title,
        original_title=original_title,
        url=str(payload.get("url") or ""),
        path=str(payload.get("path") or ""),
        index=index,
        size=parse_size_bytes(payload.get("size")),
        rating=_safe_rating(payload.get("rating")),
        time=str(payload.get("time") or ""),
        vod_id=str(payload.get("vod_id") or ""),
        external_subtitles=_map_play_item_external_subtitles(payload),
    )


def map_drive_video_to_play_item(
    payload: dict,
    *,
    index: int = 0,
    media_title: str = "",
    play_source: str = "",
) -> PlayItem:
    """Map a Video entry from the new /api/drive resolve|files API to a PlayItem.
    The backend returns a ready-to-play proxy URL, so no per-item resolution is needed."""
    title = str(payload.get("title") or payload.get("name") or "").strip()
    return PlayItem(
        title=title or "未命名",
        original_title=str(payload.get("name") or title or ""),
        url=str(payload.get("url") or ""),
        path=str(payload.get("path") or ""),
        index=index,
        size=parse_size_bytes(payload.get("size")),
        time=str(payload.get("time") or ""),
        media_title=media_title,
        play_source=play_source,
        vod_id=str(payload.get("path") or payload.get("url") or ""),
        play_id=str(payload.get("playId") or payload.get("play_id") or ""),
    )


def _map_vod_item(payload: dict) -> VodItem:
    items = [
        _map_play_item(item, index)
        for index, item in enumerate(payload.get("items") or [])
    ]
    return VodItem(
        vod_id=str(payload.get("vod_id") or ""),
        vod_name=str(payload.get("vod_name") or ""),
        path=str(payload.get("path") or ""),
        vod_pic=str(payload.get("vod_pic") or ""),
        vod_tag=str(payload.get("vod_tag") or ""),
        vod_time=str(payload.get("vod_time") or ""),
        vod_remarks=str(payload.get("vod_remarks") or ""),
        vod_play_from=str(payload.get("vod_play_from") or ""),
        vod_play_url=str(payload.get("vod_play_url") or ""),
        type_name=str(payload.get("type_name") or ""),
        category_name=str(payload.get("category_name") or ""),
        vod_content=str(payload.get("vod_content") or ""),
        vod_year=str(payload.get("vod_year") or ""),
        vod_area=str(payload.get("vod_area") or ""),
        vod_lang=str(payload.get("vod_lang") or ""),
        vod_director=str(payload.get("vod_director") or ""),
        vod_actor=str(payload.get("vod_actor") or ""),
        dbid=int(payload.get("dbid") or 0),
        type=int(payload.get("type") or 0),
        items=items,
    )

_DRIVE_DIR_EPISODE_COUNT_RE = re.compile(r"[（(]\s*(?:全)?\s*\d+\s*[集話话期](?:全)?\s*[）)]")


def clean_drive_directory_title(name: str) -> str:
    """Turn a drive directory name into a searchable media title.

    Share collections name each directory after the drama plus an episode count and
    (often) the cast, e.g. ``等不到说我爱你（82集）崔秀子＆蒋文琦``. Metadata and danmaku
    lookups only match on the drama name, so cut at the episode-count marker.
    """
    value = str(name or "").strip()
    if not value:
        return ""
    match = _DRIVE_DIR_EPISODE_COUNT_RE.search(value)
    if match is not None:
        value = value[: match.start()]
    return value.strip(" .-_·—、，,／/｜|") or str(name or "").strip()


def build_drive_grouped_sources(
    detail: VodItem,
    resource_id: str,
    directories: list,
    root_files: list,
    files_loader,
) -> tuple[list[PlaybackSourceGroup], list[list[PlayItem]]]:
    # Root files (already resolved) become the default, immediately-playable group.
    # Each top-level directory becomes a lazily-loaded group (fetched on first selection).
    # When there are no root files, the first directory is eagerly loaded so the player
    # always opens with a playable playlist.
    source_groups: list[PlaybackSourceGroup] = []
    playlists: list[list[PlayItem]] = []
    share_title = detail.vod_name or ""

    def add_populated_group(label: str, items: list, media_title: str) -> None:
        playlist = [
            map_drive_video_to_play_item(item, index=i, media_title=media_title, play_source=label)
            for i, item in enumerate(items)
            if item.get("url")
        ]
        source_groups.append(
            PlaybackSourceGroup(
                label=label,
                sources=[PlaybackSource(label=label, playlist=playlist)],
            )
        )
        playlists.append(playlist)

    if root_files:
        add_populated_group(share_title or "根目录", root_files, share_title)

    # eager_done = a populated default group already exists (root files); otherwise the first
    # directory is eagerly loaded so the player always opens with something playable.
    eager_done = bool(root_files)
    for index, directory in enumerate(directories or []):
        dir_id = str(directory.get("id") or "")
        name = str(directory.get("name") or "").strip() or f"目录 {index + 1}"
        if not eager_done and dir_id and files_loader is not None:
            try:
                items = files_loader(resource_id, dir_id) or []
            except Exception:
                items = []
            if items:
                add_populated_group(name, items, clean_drive_directory_title(name))
                eager_done = True
                continue
        # Lazy placeholder; populated by PlayerWindow on first selection.
        playlist: list[PlayItem] = []
        source_groups.append(
            PlaybackSourceGroup(
                label=name,
                sources=[PlaybackSource(label=name, playlist=playlist)],
                drive_dir_id=dir_id,
            )
        )
        playlists.append(playlist)

    return source_groups, playlists


def filter_search_results(results: list[VodItem], drive_type: str) -> list[VodItem]:
    if not drive_type:
        return list(results)
    drive_name = get_share_type_name(drive_type)
    return [
        item
        for item in results
        if item.share_type == drive_type
        or (
            drive_name
            and (drive_name in item.type_name or drive_name in item.vod_remarks)
        )
    ]


class BrowseController:
    def __init__(
        self,
        api_client,
        playback_history_loader: Callable[[str, str], object | None] | None = None,
        playback_history_saver: Callable[[str, str, dict[str, object]], None] | None = None,
    ) -> None:
        self._api_client = api_client
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver

    def _history_callbacks(self, vod_id: str, source_key: str = "csp_AList"):
        loader = None
        saver = None
        if self._playback_history_loader is not None:
            loader = lambda vod_id=vod_id, source_key=source_key: self._playback_history_loader(
                source_key, vod_id
            )
        if self._playback_history_saver is not None:
            saver = lambda payload, vod_id=vod_id, source_key=source_key: self._playback_history_saver(
                source_key, vod_id, payload
            )
        return loader, saver

    def _merge_vod_metadata(self, resolved_vod: VodItem | None, fallback_vod: VodItem) -> VodItem:
        if resolved_vod is None:
            return fallback_vod
        return VodItem(
            vod_id=resolved_vod.vod_id or fallback_vod.vod_id,
            vod_name=resolved_vod.vod_name or fallback_vod.vod_name,
            path=resolved_vod.path or fallback_vod.path,
            share_type=resolved_vod.share_type or fallback_vod.share_type,
            vod_pic=resolved_vod.vod_pic or fallback_vod.vod_pic,
            vod_tag=resolved_vod.vod_tag or fallback_vod.vod_tag,
            vod_time=resolved_vod.vod_time or fallback_vod.vod_time,
            vod_remarks=resolved_vod.vod_remarks or fallback_vod.vod_remarks,
            vod_play_from=resolved_vod.vod_play_from or fallback_vod.vod_play_from,
            vod_play_url=resolved_vod.vod_play_url or fallback_vod.vod_play_url,
            type_name=resolved_vod.type_name or fallback_vod.type_name,
            vod_content=resolved_vod.vod_content or fallback_vod.vod_content,
            vod_year=resolved_vod.vod_year or fallback_vod.vod_year,
            vod_area=resolved_vod.vod_area or fallback_vod.vod_area,
            vod_lang=resolved_vod.vod_lang or fallback_vod.vod_lang,
            vod_director=resolved_vod.vod_director or fallback_vod.vod_director,
            vod_actor=resolved_vod.vod_actor or fallback_vod.vod_actor,
            dbid=resolved_vod.dbid or fallback_vod.dbid,
            type=resolved_vod.type or fallback_vod.type,
            items=resolved_vod.items if resolved_vod.items is not None else fallback_vod.items,
        )

    def _first_play_url(self, vod: VodItem) -> str:
        if vod.items:
            return vod.items[0].url
        return vod.vod_play_url

    def resolve_folder_play_item(self, item: PlayItem) -> VodItem | None:
        try:
            payload = self._api_client.get_detail(item.vod_id)
            return _map_vod_item(payload["list"][0])
        except (KeyError, IndexError):
            return None

    def load_folder(self, path: str, page: int = 1, size: int = 50) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_vod(build_vod_list_path(path), page=page, size=size)
        items = [_map_vod_item(item) for item in payload.get("list", [])]
        return items, int(payload.get("total", len(items)))

    def search(self, keyword: str) -> list[VodItem]:
        payload = self._api_client.telegram_search(keyword)
        return [
            VodItem(
                vod_id=str(item.get("id", "")),
                vod_name=str(item.get("name", "")),
                share_type=str(item.get("type", "")),
                vod_tag="folder",
                vod_time=format_local_datetime(str(item.get("time", ""))),
                type_name=get_share_type_name(str(item.get("type", ""))) or str(item.get("type", "")),
                vod_play_from=str(item.get("channel", "")),
                vod_play_url=str(item.get("link", "")),
            )
            for item in payload
        ]

    def search_alist(self, keyword: str) -> list[VodItem]:
        payload = self._api_client.search_alist_items(keyword)
        return [_map_vod_item(item) for item in payload.get("list", [])]

    def build_playlist_from_folder(
        self,
        folder_items: list[VodItem],
        clicked_vod_id: str,
    ) -> tuple[list[PlayItem], int]:
        playlist: list[PlayItem] = []
        start_index = 0
        _matched = False
        for item in folder_items:
            if item.type != 2:
                continue
            index = len(playlist)
            playlist_item = PlayItem(
                title=item.vod_name,
                original_title=item.vod_name,
                url=item.vod_play_url,
                path=item.path,
                index=index,
                size=parse_size_bytes(item.vod_remarks) if item.vod_tag == "file" else 0,
                rating=0.0,
                time=str(item.vod_time or ""),
                vod_id=item.vod_id,
            )
            playlist.append(playlist_item)
            if item.vod_id == clicked_vod_id and not _matched:
                start_index = index
                _matched = True
        return playlist, start_index

    def resolve_search_result(self, item: VodItem) -> str:
        return self._api_client.resolve_share_link(item.vod_play_url)

    def rename_file(self, item: VodItem, name: str) -> None:
        self._api_client.rename_video(_video_id_from_vod_id(item.vod_id), name)

    def delete_file(self, item: VodItem) -> None:
        self._api_client.delete_video(_video_id_from_vod_id(item.vod_id))

    def build_request_from_detail(self, vod_id: str, source_key: str = "csp_AList") -> OpenPlayerRequest:
        payload = self._api_client.get_detail(vod_id)
        detail = _map_vod_item(payload["list"][0])
        if not detail.items:
            raise ValueError(f"没有可播放的项目: {detail.vod_name}")
        source_vod_id = str(detail.vod_id or vod_id)
        history_loader, history_saver = self._history_callbacks(source_vod_id, source_key)
        return OpenPlayerRequest(
            vod=detail,
            playlist=detail.items,
            clicked_index=0,
            source_kind="browse",
            source_key=source_key,
            source_mode="detail",
            source_vod_id=vod_id,
            use_local_history=False,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )

    def build_request_from_folder_item(
        self,
        clicked_item: VodItem,
        folder_items: list[VodItem],
    ) -> OpenPlayerRequest:
        playlist, clicked_index = self.build_playlist_from_folder(folder_items, clicked_item.vod_id)
        if not playlist:
            raise ValueError(f"没有可播放的项目: {clicked_item.vod_name}")
        clicked_playlist_item = playlist[clicked_index]
        resolved_vod = self._merge_vod_metadata(self.resolve_folder_play_item(clicked_playlist_item), clicked_item)
        resolved_vod.vod_id = clicked_item.vod_id
        clicked_playlist_item.url = self._first_play_url(resolved_vod)
        if not clicked_playlist_item.url:
            raise ValueError(f"没有可用的播放地址: {clicked_item.vod_name}")
        if not clicked_playlist_item.external_subtitles and resolved_vod.items and resolved_vod.items[0].external_subtitles:
            clicked_playlist_item.external_subtitles = list(resolved_vod.items[0].external_subtitles)
        history_loader, history_saver = self._history_callbacks(clicked_item.vod_id)
        return OpenPlayerRequest(
            vod=resolved_vod,
            playlist=playlist,
            clicked_index=clicked_index,
            source_kind="browse",
            source_key="csp_AList",
            source_mode="folder",
            source_path=clicked_item.path.rsplit("/", 1)[0] or "/",
            source_vod_id=clicked_item.vod_id,
            source_clicked_vod_id=clicked_item.vod_id,
            detail_resolver=self.resolve_folder_play_item,
            resolved_vod_by_id={resolved_vod.vod_id: resolved_vod},
            use_local_history=False,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )
