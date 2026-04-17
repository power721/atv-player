from urllib.parse import urlparse

from atv_player.models import HistoryRecord, PlayItem


def _basename(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path.rsplit("/", 1)[-1]


def resolve_resume_index(
    history: HistoryRecord | None,
    playlist: list[PlayItem],
    clicked_index: int,
) -> int:
    if history is None:
        return clicked_index
    if history.episode_url:
        target = _basename(history.episode_url)
        for index, item in enumerate(playlist):
            if _basename(item.url) == target:
                return index
    if 0 <= history.episode < len(playlist):
        return history.episode
    return clicked_index
