"""TMDB 季/集对齐方法论(本地对齐层)的优先级链与安全退化。"""

from atv_player.metadata.season_alignment import (
    ALIGNMENT_EXPLICIT,
    ALIGNMENT_FLAT,
    ALIGNMENT_FLAT_OFFSET,
    ALIGNMENT_TITLE_MARKER,
    ALIGNMENT_UNIQUE_SEASON,
    resolve_season_alignment,
)
from atv_player.models import PlayItem


def test_explicit_file_seasons_group_directly() -> None:
    alignment = resolve_season_alignment(
        [2, 2, 2],
        [5, 6, 7],
        marker_season=2,
        tmdb_season_episode_counts={1: 12, 2: 12},
    )
    assert alignment.pairs == [(2, 5), (2, 6), (2, 7)]
    assert alignment.mode == ALIGNMENT_EXPLICIT


def test_explicit_season_outside_tmdb_entry_stays_unresolved() -> None:
    # 文件声明 S05 但 TMDB 只有 S1-S3:保留显式断言不强改,该条目解析不出
    # 对应集(上层回退/放弃),也绝不让唯一季规则把它们重映射到 S1。
    alignment = resolve_season_alignment(
        [5, 5],
        [1, 2],
        marker_season=5,
        tmdb_season_episode_counts={1: 10, 2: 10, 3: 10},
    )
    assert alignment.pairs == [(5, 1), (5, 2)]
    assert alignment.mode == ALIGNMENT_EXPLICIT


def test_marker_season_wins_over_unique_season_inference() -> None:
    # 标题说第二季、条目只有 S1(独立续作条目常见):标记是本地证据,
    # 不被唯一季推断改写为 S1——保留给上层回退到裸条目。
    alignment = resolve_season_alignment(
        [None, None],
        [1, 2],
        marker_season=2,
        tmdb_season_episode_counts={1: 12},
    )
    assert alignment.pairs == [(2, 1), (2, 2)]
    assert alignment.mode == ALIGNMENT_TITLE_MARKER


def test_marker_season_fills_unknown_files() -> None:
    alignment = resolve_season_alignment(
        [None, None],
        [1, 2],
        marker_season=2,
        tmdb_season_episode_counts={1: 12, 2: 12},
    )
    assert alignment.pairs == [(2, 1), (2, 2)]
    assert alignment.mode == ALIGNMENT_TITLE_MARKER


def test_marker_season_fills_unknowns_alongside_explicit_seasons() -> None:
    # 混合目录:个别文件带显式季(第一季子目录),其余平铺文件由标题标记补齐——
    # 不同文件本就属于不同季,标记填充不因显式存在而弃用。
    alignment = resolve_season_alignment(
        [1, None],
        [1, 2],
        marker_season=2,
        tmdb_season_episode_counts={1: 12, 2: 12},
    )
    assert alignment.pairs == [(1, 1), (2, 2)]
    assert alignment.mode == ALIGNMENT_EXPLICIT


def test_unique_tmdb_season_fills_unknown() -> None:
    # TMDB 条目只有一个非特别季:无标记文件可安全推断为该季。
    alignment = resolve_season_alignment(
        [None, None, None],
        [1, 2, 3],
        tmdb_season_episode_counts={1: 8},
    )
    assert alignment.pairs == [(1, 1), (1, 2), (1, 3)]
    assert alignment.mode == ALIGNMENT_UNIQUE_SEASON


def test_special_season_does_not_count_for_unique_inference() -> None:
    # S0(特别篇)不参与"唯一季"判定。
    alignment = resolve_season_alignment(
        [None],
        [1],
        tmdb_season_episode_counts={0: 2, 1: 8},
    )
    assert alignment.pairs == [(1, 1)]
    assert alignment.mode == ALIGNMENT_UNIQUE_SEASON


def test_flat_offset_maps_sequential_playlist_across_seasons() -> None:
    # 平铺 1..50 恰好等于 S1(24)+S2(26):严格连续+总数精确 → 按偏移分配。
    alignment = resolve_season_alignment(
        [None] * 50,
        list(range(1, 51)),
        tmdb_season_episode_counts={1: 24, 2: 26},
    )
    assert alignment.mode == ALIGNMENT_FLAT_OFFSET
    assert alignment.pairs[0] == (1, 1)
    assert alignment.pairs[23] == (1, 24)
    assert alignment.pairs[24] == (2, 1)
    assert alignment.pairs[49] == (2, 26)


def test_flat_offset_rejected_when_total_does_not_match_exactly() -> None:
    # 49 集 ≠ 24+26:不猜,退未知。
    alignment = resolve_season_alignment(
        [None] * 49,
        list(range(1, 50)),
        tmdb_season_episode_counts={1: 24, 2: 26},
    )
    assert alignment.mode == ALIGNMENT_FLAT
    assert all(pair is None for pair in alignment.pairs)


def test_flat_offset_rejected_when_episodes_not_sequential() -> None:
    # 集号有缺口(E2、E4..):覆盖不满 100%,退未知(多季条目不走唯一季推断)。
    alignment = resolve_season_alignment(
        [None, None],
        [2, 4],
        tmdb_season_episode_counts={1: 2, 2: 2},
    )
    assert alignment.mode == ALIGNMENT_FLAT
    assert alignment.pairs == [None, None]


def test_flat_offset_rejected_when_multiple_windows_match() -> None:
    # 24 集既能是 S1 也能是 S2:歧义不自动选。
    alignment = resolve_season_alignment(
        [None] * 24,
        list(range(1, 25)),
        tmdb_season_episode_counts={1: 24, 2: 24},
    )
    assert alignment.mode == ALIGNMENT_FLAT
    assert all(pair is None for pair in alignment.pairs)


def test_unknown_season_is_not_season_one() -> None:
    # 核心铁律:无任何本地信号且 TMDB 多季时,"无法判断"不等于第 1 季。
    alignment = resolve_season_alignment(
        [None, None],
        [1, 2],
        tmdb_season_episode_counts={1: 12, 2: 12},
    )
    assert alignment.pairs == [None, None]
    assert alignment.mode == ALIGNMENT_FLAT


def test_episode_unknown_keeps_pair_none() -> None:
    alignment = resolve_season_alignment(
        [2],
        [None],
        marker_season=2,
        tmdb_season_episode_counts={1: 12, 2: 12},
    )
    assert alignment.pairs == [None]


def test_without_tmdb_counts_local_rules_still_apply() -> None:
    # 尚无 TMDB 结构信息时只做本地规则(显式/标记),不做唯一季与平铺偏移。
    alignment = resolve_season_alignment(
        [None, None],
        [1, 2],
        marker_season=3,
        tmdb_season_episode_counts=None,
    )
    assert alignment.pairs == [(3, 1), (3, 2)]
    assert alignment.mode == ALIGNMENT_TITLE_MARKER


def test_playitem_season_extraction_uses_title_then_path() -> None:
    from atv_player.metadata.season_alignment import extract_item_season

    assert extract_item_season(PlayItem(title="第01集", url="x")) is None
    assert extract_item_season(PlayItem(title="S02E05.mkv", url="x")) == 2
    assert (
        extract_item_season(
            PlayItem(title="第05集", url="x", path="/share/剧名/S01/第05集.mkv")
        )
        == 1
    )
