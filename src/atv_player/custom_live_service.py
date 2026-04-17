from __future__ import annotations

from pathlib import Path
from typing import Protocol

from atv_player.m3u_parser import ParsedChannel, ParsedGroup, ParsedPlaylist, parse_m3u
from atv_player.models import (
    DoubanCategory,
    LiveSourceChannelView,
    OpenPlayerRequest,
    PlayItem,
    VodItem,
)


class _HttpTextClient(Protocol):
    def get_text(self, url: str) -> str:
        ...


class CustomLiveService:
    def __init__(self, repository, http_client: _HttpTextClient) -> None:
        self._repository = repository
        self._http_client = http_client

    def list_sources(self):
        return self._repository.list_sources()

    def add_remote_source(self, url: str, display_name: str):
        return self._repository.add_source("remote", url, display_name)

    def add_local_source(self, path: str, display_name: str):
        return self._repository.add_source("local", path, display_name)

    def add_manual_source(self, display_name: str):
        return self._repository.add_source("manual", "", display_name)

    def rename_source(self, source_id: int, display_name: str) -> None:
        source = self._repository.get_source(source_id)
        self._repository.update_source(
            source.id,
            display_name=display_name,
            enabled=source.enabled,
            source_value=source.source_value,
            cache_text=source.cache_text,
            last_error=source.last_error,
            last_refreshed_at=source.last_refreshed_at,
        )

    def delete_source(self, source_id: int) -> None:
        self._repository.delete_source(source_id)

    def set_source_enabled(self, source_id: int, enabled: bool) -> None:
        self._repository.set_source_enabled(source_id, enabled)

    def list_manual_entries(self, source_id: int):
        return self._repository.list_manual_entries(source_id)

    def load_categories(self) -> list[DoubanCategory]:
        return [
            DoubanCategory(type_id=f"custom:{item.id}", type_name=item.display_name)
            for item in self._repository.list_sources()
            if item.enabled
        ]

    def load_items(self, category_id: str, page: int) -> tuple[list[VodItem], int]:
        del page
        source_id = int(category_id.split(":", 1)[1])
        source = self._repository.get_source(source_id)
        playlist = self._load_playlist(source)
        if playlist.groups:
            items = [
                VodItem(
                    vod_id=f"custom-folder:{source.id}:{group.key}",
                    vod_name=group.name,
                    vod_tag="folder",
                )
                for group in playlist.groups
            ]
            return items, len(items)
        items = [
            VodItem(
                vod_id=f"custom-channel:{source.id}:{channel.key}",
                vod_name=channel.name,
                vod_tag="file",
                vod_pic=channel.logo_url,
            )
            for channel in playlist.ungrouped_channels
        ]
        return items, len(items)

    def load_folder_items(self, vod_id: str) -> tuple[list[VodItem], int]:
        _prefix, source_id_text, group_key = vod_id.split(":", 2)
        source = self._repository.get_source(int(source_id_text))
        playlist = self._load_playlist(source)
        group = next(item for item in playlist.groups if item.key == group_key)
        items = [
            VodItem(
                vod_id=f"custom-channel:{source.id}:{channel.key}",
                vod_name=channel.name,
                vod_tag="file",
                vod_pic=channel.logo_url,
            )
            for channel in group.channels
        ]
        return items, len(items)

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        _prefix, source_id_text, channel_key = vod_id.split(":", 2)
        source = self._repository.get_source(int(source_id_text))
        playlist = self._load_playlist(source)
        for view in self._iter_channel_views(source.id, playlist):
            if view.channel_id == channel_key:
                return self._build_request_from_channel(view)
        raise ValueError(f"没有可播放的项目: {vod_id}")

    def refresh_source(self, source_id: int) -> None:
        source = self._repository.get_source(source_id)
        if source.source_type == "manual":
            return
        try:
            text = self._read_source_text(source)
        except Exception as exc:
            self._repository.update_source(
                source.id,
                display_name=source.display_name,
                enabled=source.enabled,
                source_value=source.source_value,
                cache_text=source.cache_text,
                last_error=str(exc),
                last_refreshed_at=source.last_refreshed_at,
            )
            raise
        self._repository.update_source(
            source.id,
            display_name=source.display_name,
            enabled=source.enabled,
            source_value=source.source_value,
            cache_text=text,
            last_error="",
            last_refreshed_at=max(1, source.last_refreshed_at + 1),
        )

    def _build_request_from_channel(self, view: LiveSourceChannelView) -> OpenPlayerRequest:
        return OpenPlayerRequest(
            vod=VodItem(vod_id=view.channel_id, vod_name=view.channel_name, vod_pic=view.logo_url, detail_style="live"),
            playlist=[
                PlayItem(
                    title=view.channel_name,
                    url=view.stream_url,
                    vod_id=view.channel_id,
                    index=0,
                    headers=dict(view.headers),
                )
            ],
            clicked_index=0,
            source_kind="live",
            source_mode="custom",
            source_vod_id=view.channel_id,
            use_local_history=False,
        )

    def _load_playlist(self, source) -> ParsedPlaylist:
        if source.source_type == "manual":
            return self._load_manual_playlist(source.id)
        if source.cache_text:
            return parse_m3u(source.cache_text)
        text = self._read_source_text(source)
        self._repository.update_source(
            source.id,
            display_name=source.display_name,
            enabled=source.enabled,
            source_value=source.source_value,
            cache_text=text,
            last_error="",
            last_refreshed_at=max(1, source.last_refreshed_at + 1),
        )
        return parse_m3u(text)

    def _read_source_text(self, source) -> str:
        if source.source_type == "remote":
            return self._http_client.get_text(source.source_value)
        if source.source_type == "local":
            return Path(source.source_value).read_text(encoding="utf-8")
        raise ValueError(f"不支持的直播源类型: {source.source_type}")

    def _load_manual_playlist(self, source_id: int) -> ParsedPlaylist:
        playlist = ParsedPlaylist()
        groups: dict[str, ParsedGroup] = {}
        for entry in self._repository.list_manual_entries(source_id):
            channel = ParsedChannel(key=f"manual-{entry.id}", name=entry.channel_name, url=entry.stream_url)
            if entry.group_name:
                group = groups.get(entry.group_name)
                if group is None:
                    group = ParsedGroup(key=f"group-{len(groups)}", name=entry.group_name)
                    groups[entry.group_name] = group
                    playlist.groups.append(group)
                group.channels.append(channel)
            else:
                playlist.ungrouped_channels.append(channel)
        return playlist

    def _iter_channel_views(self, source_id: int, playlist: ParsedPlaylist):
        for channel in playlist.ungrouped_channels:
            yield LiveSourceChannelView(
                source_id=source_id,
                channel_id=channel.key,
                group_key="",
                channel_name=channel.name,
                stream_url=channel.url,
                logo_url=channel.logo_url,
                headers=dict(channel.headers),
            )
        for group in playlist.groups:
            for channel in group.channels:
                yield LiveSourceChannelView(
                    source_id=source_id,
                    channel_id=channel.key,
                    group_key=group.key,
                    channel_name=channel.name,
                    stream_url=channel.url,
                    logo_url=channel.logo_url,
                    headers=dict(channel.headers),
                )
