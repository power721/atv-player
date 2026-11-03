"""TMDB 季/集对齐方法论(本地对齐层)。

借鉴 WebHTV 的 "元数据季 ≠ 可播季" 方法论,核心铁律:

1. 文件/目录是播放事实源,TMDB 只增强不创建;
2. 解析优先级链:显式季号 → 标题季标记 → TMDB 唯一季 → 集数精确相等的
   平铺跨季偏移 → 退化未知;
3. "无法判断"不是第 1 季也不是全季——未知集不参与 TMDB 标题对齐,保留
   原文件名,宁可不对齐也不错绑;
4. 平铺偏移映射只在 严格连续 + 总数精确 + 覆盖率 100% 时自动生效,多解
   歧义时退化未知。

模块保持纯函数(无 I/O、无网络),便于单测与在 app.py 增强器 /
episode_title_resolver 两处复用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from atv_player.episode_titles import extract_season_number
from atv_player.models import PlayItem

ALIGNMENT_EXPLICIT = "explicit"
ALIGNMENT_TITLE_MARKER = "title_marker"
ALIGNMENT_UNIQUE_SEASON = "unique_season"
ALIGNMENT_FLAT_OFFSET = "flat_offset"
ALIGNMENT_FLAT = "flat"


@dataclass(frozen=True, slots=True)
class SeasonAlignment:
    """季/集对齐结果。

    item_seasons / item_episodes / pairs 按播放列表下标一一对应;
    ``pairs[i]`` 为 None 表示该集季号或集号未知,不参与 TMDB 对齐。
    """

    item_seasons: list[int | None]
    item_episodes: list[int | None]
    pairs: list[tuple[int, int] | None]
    mode: str
    reason: str = ""


def extract_item_season(item: PlayItem) -> int | None:
    """文件季号:对齐盖章(``item.season_number``)优先,其次文件自身显式标记
    (original_title → title → path)。盖章值来自已验证的对齐结果,重算路径
    (如写回后的 ``_season_episode_pairs``)据此保持一致,不再退回未知。"""
    stamped = int(getattr(item, "season_number", 0) or 0)
    if stamped >= 1:
        return stamped
    for value in (item.original_title, item.title, item.path):
        season_number = extract_season_number(value)
        if season_number is not None:
            return season_number
    return None


def extract_playlist_item_seasons(playlist: Sequence[PlayItem]) -> list[int | None]:
    return [extract_item_season(item) for item in playlist]


def _non_special_season_counts(
    tmdb_season_episode_counts: Mapping[int, int] | None,
) -> dict[int, int]:
    if not tmdb_season_episode_counts:
        return {}
    return {
        int(season): int(count)
        for season, count in tmdb_season_episode_counts.items()
        if int(season) >= 1 and int(count) > 0
    }


def _find_unique_flat_offset_window(
    total_episodes: int,
    season_counts: dict[int, int],
) -> list[int] | None:
    """找出连续 TMDB 季窗口使其集数之和精确等于 total_episodes。

    返回命中的季号列表;多个窗口命中(歧义)或没有窗口命中时返回 None。
    """
    seasons = sorted(season_counts)
    windows: list[list[int]] = []
    for start_index, _start_season in enumerate(seasons):
        window: list[int] = []
        remaining = total_episodes
        for season in seasons[start_index:]:
            count = season_counts[season]
            window.append(season)
            remaining -= count
            if remaining < 0:
                break
            if remaining == 0:
                windows.append(list(window))
                break
    if len(windows) == 1:
        return windows[0]
    return None


def resolve_season_alignment(
    item_seasons: Sequence[int | None],
    item_episodes: Sequence[int | None],
    *,
    marker_season: int | None = None,
    tmdb_season_episode_counts: Mapping[int, int] | None = None,
) -> SeasonAlignment:
    """按优先级链解析播放列表的 (season, episode) 对。

    :param item_seasons: 每个文件自身的显式季号(未知为 None)。
    :param item_episodes: 每个文件解析出的集号(未知为 None)。
    :param marker_season: 标题(剧名/备注)里的季标记,用于填未知;与显式季冲突时弃用。
    :param tmdb_season_episode_counts: TMDB 条目各季集数(season → episode_count);
        提供时启用 唯一季推断、显式季校验 与 平铺跨季偏移;None 表示尚无 TMDB
        结构信息,只做本地规则。
    """
    seasons: list[int | None] = [
        int(value) if value is not None else None for value in item_seasons
    ]
    episodes: list[int | None] = [
        int(value) if value is not None else None for value in item_episodes
    ]
    season_counts = _non_special_season_counts(tmdb_season_episode_counts)
    explicit_seasons = {value for value in seasons if value is not None}
    reason = ""
    mode = ALIGNMENT_EXPLICIT if explicit_seasons else ALIGNMENT_FLAT

    if marker_season is not None and marker_season >= 1 and None in seasons:
        # 标题季标记是本地证据,不对 TMDB 条目校验(独立续作条目会把第二季
        # 建模为自身 S1,标记"不在条目里"恰说明条目可能不是目标);也不因与
        # 个别文件的显式季不同而弃用——混合目录(第一季子目录+第二季平铺)
        # 里不同文件本就属于不同季。
        if explicit_seasons and marker_season not in explicit_seasons:
            reason = f"marker_season_differs_from_explicit:{marker_season}"
        seasons = [marker_season if value is None else value for value in seasons]
        if explicit_seasons:
            mode = ALIGNMENT_EXPLICIT
        else:
            mode = ALIGNMENT_TITLE_MARKER

    if None in seasons and len(season_counts) == 1:
        # 唯一季推断:无冲突证据即可——所有已知季(含标记填充)与唯一季一致时,
        # 同播放列表里无信号文件可跟着同类文件走;标记指向别处(独立续作条目)
        # 或已知季不同时禁止,防错季重映射。
        only_season = next(iter(season_counts))
        known_season_values = {value for value in seasons if value is not None}
        if not known_season_values or known_season_values == {only_season}:
            seasons = [only_season if value is None else value for value in seasons]
            mode = ALIGNMENT_UNIQUE_SEASON

    pairs: list[tuple[int, int] | None] = [
        (season, episode) if season is not None and episode is not None else None
        for season, episode in zip(seasons, episodes, strict=True)
    ]

    if (
        len(season_counts) > 1
        and all(season is None for season in seasons)
        and all(episode is not None for episode in episodes)
    ):
        known_episodes = sorted(int(value) for value in episodes)  # type: ignore[arg-type]
        total = len(known_episodes)
        if known_episodes == list(range(1, total + 1)):
            window = _find_unique_flat_offset_window(total, season_counts)
            if window is not None:
                offset_season_by_episode: dict[int, tuple[int, int]] = {}
                episode_cursor = 1
                for season in window:
                    for within_season in range(1, season_counts[season] + 1):
                        offset_pair = (season, within_season)
                        offset_season_by_episode[episode_cursor] = offset_pair
                        episode_cursor += 1
                pairs = [offset_season_by_episode[int(value)] for value in episodes]  # type: ignore[arg-type]
                mode = ALIGNMENT_FLAT_OFFSET

    return SeasonAlignment(
        item_seasons=seasons,
        item_episodes=episodes,
        pairs=pairs,
        mode=mode,
        reason=reason,
    )
