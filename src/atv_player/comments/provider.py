from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from atv_player.comments.bangumi import BangumiCommentsClient, build_bangumi_comments_loader
from atv_player.comments.douban import (
    DoubanCommentsClient,
    build_douban_comments_loader,
    douban_kind_from_type_name,
)
from atv_player.danmaku.discovery.douban import DoubanSubject
from atv_player.heat.identity import heat_identity_from_vod
from atv_player.models import PlayItem, VodItem

CommentsLoader = Callable[[dict[str, object]], dict[str, object]]

_DOUBAN_SOURCE_LABEL = "豆瓣短评"
_BANGUMI_SOURCE_LABEL = "Bangumi 吐槽"


@dataclass(frozen=True, slots=True)
class CommentsSource:
    """一个可用的评论源:label 供源切换按钮展示,loader 走评论对话框协议。"""

    key: str
    label: str
    loader: CommentsLoader


class MetadataCommentsProvider:
    """刮削成功后解析豆瓣/Bangumi 评论源。

    ID 优先取刮削元数据里的外部 ID(豆瓣 dbid/detail_fields、Bangumi detail_fields),
    缺失时按「标题+年份」现搜兜底;解析结果按「标题+年份」内存缓存,同一播放器
    会话内不重复现搜。搜索失败静默降级(另一源仍可用),评论列表加载失败才对用户报错。
    """

    def __init__(self, douban_client: DoubanCommentsClient, bangumi_client: BangumiCommentsClient) -> None:
        self._douban = douban_client
        self._bangumi = bangumi_client
        self._resolved: dict[str, dict[str, tuple[str, str] | None]] = {}

    def resolve(self, vod: VodItem | None, item: PlayItem | None = None) -> list[CommentsSource]:
        vod = vod or getattr(item, "vod", None)
        if vod is None:
            return []
        identity = heat_identity_from_vod(vod, item)
        if identity is None or not identity.title:
            return []
        cache_key = f"{identity.title.strip()}\x1f{identity.year.strip()}"
        resolved = self._resolved.get(cache_key)
        if resolved is None:
            resolved = self._resolve_ids(identity.title, identity.year, identity.external_ids)
            self._resolved[cache_key] = resolved
        sources: list[CommentsSource] = []
        douban = resolved.get("douban")
        if douban is not None:
            subject_id, kind = douban
            sources.append(
                CommentsSource(
                    "douban",
                    _DOUBAN_SOURCE_LABEL,
                    build_douban_comments_loader(self._douban, subject_id, kind),
                )
            )
        bangumi = resolved.get("bangumi")
        if bangumi is not None:
            subject_id, _kind = bangumi
            sources.append(
                CommentsSource(
                    "bangumi",
                    _BANGUMI_SOURCE_LABEL,
                    build_bangumi_comments_loader(self._bangumi, subject_id),
                )
            )
        return sources

    # --- ID 解析 --------------------------------------------------------

    def _resolve_ids(
        self, title: str, year: str, external_ids: dict[str, str]
    ) -> dict[str, tuple[str, str] | None]:
        resolved: dict[str, tuple[str, str] | None] = {}
        douban_id = str(external_ids.get("douban") or "").strip()
        if douban_id:
            # dbid 来源不知道电影/剧集:先走 movie(rexxar 对剧集 301 到 tv),404 再补 tv
            resolved["douban"] = (douban_id, "movie")
        else:
            subject = self._search_douban(title, year)
            if subject is not None:
                resolved["douban"] = (subject.douban_id, douban_kind_from_type_name(subject.type_name))
        bangumi_id = str(external_ids.get("bangumi") or "").strip()
        if not bangumi_id:
            bangumi_id = self._search_bangumi(title, year)
        if bangumi_id:
            resolved["bangumi"] = (bangumi_id, "")
        return resolved

    def _search_douban(self, title: str, year: str) -> DoubanSubject | None:
        try:
            subjects = self._douban.search_subjects(title)
        except Exception:
            return None
        return _pick_douban_subject(subjects, title, year)

    def _search_bangumi(self, title: str, year: str) -> str:
        try:
            entries = self._bangumi.search_subjects(title)
        except Exception:
            return ""
        return _pick_bangumi_subject(entries, year)


def _pick_douban_subject(subjects: list[DoubanSubject], title: str, year: str) -> DoubanSubject | None:
    """标题精确等值优先,其次包含;同分内年份一致的排前(避免同名错剧)。"""
    if not subjects:
        return None
    normalized = title.strip().casefold()

    def score(subject: DoubanSubject) -> tuple[int, int]:
        name = subject.title.strip().casefold()
        exact = name == normalized
        contains = normalized in name or name in normalized
        title_score = 2 if exact else (1 if contains else 0)
        year_match = 1 if (year and subject.year == year) else 0
        return (title_score, year_match)

    ranked = sorted(subjects, key=score, reverse=True)
    best = ranked[0]
    if score(best)[0] == 0:
        return None
    return best


def _pick_bangumi_subject(entries: list[dict], year: str) -> str:
    """搜索相关性通常足够;仅在存在年份字段时做同分加票,取第一个的 id。"""
    for entry in entries:
        subject_id = str(entry.get("id") or "").strip()
        if not subject_id:
            continue
        entry_year = str(entry.get("date") or "")[:4]
        if not year or not entry_year or entry_year == year:
            return subject_id
    for entry in entries:
        subject_id = str(entry.get("id") or "").strip()
        if subject_id:
            return subject_id
    return ""
