# ruff: noqa: E501
"""服务端追剧(msub)播放控制器。

继续播放/搜索播放直达服务端订阅:playlist 为"当前可播"各集(present),
每集 URL 懒解析——切集时经 async playback_loader 调 /play/{token}?id=msubep-{sub}-{ep},
服务端负责多源故障转移;本地只持有逻辑集 id(msubep-),解析出的直链不落历史。
进度回传约定:vod_id=msub:{subId}、episodeUrl 含 msubep-{subId}-{ep}
(服务端 watchedEpisode 按 History.vodId=msub:{id} + 正则解析集号)。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable

from atv_player.models import (
    ExternalSubtitleOption,
    HistoryRecord,
    OpenPlayerRequest,
    PlayItem,
    VodItem,
)

logger = logging.getLogger(__name__)

_MSUBEP_RE = re.compile(r"msubep-(\d+)-(\d+)")


def parse_msub_vod_id(vod_id: str) -> int:
    """'msub:{id}' 或裸数字 → 订阅 id;解析失败返回 0。"""
    text = str(vod_id or "").strip()
    if text.startswith("msub:"):
        text = text[len("msub:"):]
    if not text.isdigit():
        return 0
    return int(text) if text else 0


class MsubController:
    def __init__(
        self,
        api_client,
        playback_history_loader: Callable[[str], HistoryRecord | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self._api_client = api_client
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver

    def build_request(self, vod_id: str, start_episode: int = 0) -> OpenPlayerRequest:
        subscription_id = parse_msub_vod_id(vod_id)
        if subscription_id <= 0:
            raise ValueError(f"无效的服务端追剧标识: {vod_id}")
        payload = self._api_client.get_media_subscription_detail(subscription_id)
        subscription = payload.get("subscription") or {}
        media = payload.get("media") or {}
        name = str(subscription.get("name") or media.get("name") or "").strip() or f"服务端追剧 {subscription_id}"
        playlist = self._build_playlist(subscription_id, name, payload.get("episodes") or [])
        if not playlist:
            raise ValueError(f"《{name}》暂无可播剧集(服务端资源尚未就绪)")
        clicked_index = 0
        if start_episode > 0:
            clicked_index = max(
                0,
                next(
                    (index for index, item in enumerate(playlist) if _episode_number_of(item) >= start_episode),
                    len(playlist) - 1,
                ),
            )
        cover = str(subscription.get("cover") or media.get("cover") or "")
        official_episodes = int(subscription.get("officialEpisodes") or 0)
        official_total = int(subscription.get("officialTotal") or 0)
        remarks = "服务端追剧"
        if official_episodes > 0:
            remarks = f"已播 {official_episodes}/{official_total or '?'} 集"
        vod = VodItem(
            vod_id=f"msub:{subscription_id}",
            vod_name=name,
            vod_pic=cover,
            vod_remarks=remarks,
            vod_content=str(media.get("overview") or ""),
            vod_year=str(media.get("year") or ""),
        )
        history_loader = None
        history_saver = None
        if self._playback_history_loader is not None:
            def history_loader(source_vod_id=vod.vod_id):
                return self._playback_history_loader(source_vod_id)
        if self._playback_history_saver is not None:
            def history_saver(history_payload, source_vod_id=vod.vod_id):
                return self._playback_history_saver(source_vod_id, history_payload)
        return OpenPlayerRequest(
            vod=vod,
            playlist=playlist,
            clicked_index=clicked_index,
            source_kind="msub",
            # source_key=服务器地址:与追更绑定(apply_backend_signal)和播放进度匹配
            # (_player_following_matches_record 按 kind+key+vod_id 三元组)保持一致。
            source_key=str(getattr(self._api_client, "base_url", "") or ""),
            source_mode="detail",
            source_vod_id=vod.vod_id,
            use_local_history=False,
            playback_loader=self.load_playback_item,
            async_playback_loader=True,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
            initial_log_message="服务端追剧 · 换台/切集时按需解析播放源",
        )

    def _build_playlist(self, subscription_id: int, name: str, episodes: list) -> list[PlayItem]:
        playlist: list[PlayItem] = []
        for entry in episodes:
            if not isinstance(entry, dict) or not entry.get("present"):
                continue
            episode_number = int(entry.get("episode") or 0)
            if episode_number <= 0:
                continue
            episode_title = str(entry.get("title") or "").strip()
            display_title = f"第{episode_number}集" + (f" {episode_title}" if episode_title else "")
            playlist.append(
                PlayItem(
                    title=display_title,
                    url="",
                    original_url=f"msubep-{subscription_id}-{episode_number}",
                    vod_id=f"msub:{subscription_id}",
                    play_id=f"msubep-{subscription_id}-{episode_number}",
                    media_title=name,
                    episode_display_title=episode_title,
                    video_cover_override=str(entry.get("still") or ""),
                )
            )
        return playlist

    def load_playback_item(self, item: PlayItem) -> None:
        match = _MSUBEP_RE.search(str(item.play_id or item.original_url or ""))
        if match is None:
            raise ValueError(f"缺少服务端追剧集标识: {item.title}")
        subscription_id = int(match.group(1))
        episode_number = int(match.group(2))
        payload = self._api_client.resolve_msub_episode(subscription_id, episode_number)
        url = str(payload.get("url") or "").strip()
        if not url:
            raise ValueError(f"{item.title} 没有可用播放地址")
        item.url = url
        headers = payload.get("header") or {}
        if isinstance(headers, dict):
            item.headers = {str(key): str(value) for key, value in headers.items() if str(value or "").strip()}
        item.original_url = f"msubep-{subscription_id}-{episode_number}"
        subtitles = self._parse_subtitles(payload)
        if subtitles:
            item.external_subtitles = subtitles
        # 网盘直链带时效,history 里只保留 msubep- 逻辑 id(经 play_id 生成),
        # 切集/续播时永远重新解析,绝不复用过期直链。

    def _parse_subtitles(self, payload: dict) -> list[ExternalSubtitleOption]:
        options: list[ExternalSubtitleOption] = []
        for entry in list(payload.get("subs") or []):
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or entry.get("link") or "").strip()
            if not url:
                continue
            options.append(
                ExternalSubtitleOption(
                    name=str(entry.get("name") or entry.get("title") or entry.get("lang") or "字幕"),
                    lang=str(entry.get("lang") or entry.get("language") or ""),
                    url=url,
                    format=str(entry.get("format") or entry.get("ext") or ""),
                    source="msub",
                )
            )
        primary = str(payload.get("subt") or "").strip()
        if primary and not any(option.url == primary for option in options):
            options.insert(0, ExternalSubtitleOption(name="字幕", lang="", url=primary, source="msub"))
        return options


def _episode_number_of(item: PlayItem) -> int:
    match = _MSUBEP_RE.search(str(item.play_id or item.original_url or ""))
    return int(match.group(2)) if match else 0
