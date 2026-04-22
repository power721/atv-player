from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import quote, urljoin

from atv_player.proxy.adblock import is_ad_segment
from atv_player.proxy.session import PlaylistSegment, ProxySessionRegistry

_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')


@dataclass(slots=True, frozen=True)
class RewrittenPlaylist:
    text: str
    is_master: bool


def rewrite_playlist(
    *,
    token: str,
    playlist_url: str,
    content: str,
    session_registry: ProxySessionRegistry,
    proxy_base_url: str,
) -> RewrittenPlaylist:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    session = session_registry.get(token)
    new_segments: list[PlaylistSegment] = []
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        output: list[str] = []
        for line in lines:
            if line.startswith("#"):
                output.append(line)
                continue
            child_url = urljoin(playlist_url, line)
            child_token = session_registry.create_session(child_url, session.headers)
            output.append(f"{proxy_base_url}/m3u?token={quote(child_token)}")
        return RewrittenPlaylist(text="\n".join(output) + "\n", is_master=True)

    output: list[str] = []
    pending_duration: float | None = None
    segment_index = 0
    for line in lines:
        if line.startswith("#EXTINF:"):
            pending_duration = float(line.split(":", 1)[1].split(",", 1)[0])
            output.append(line)
            continue
        if line.startswith("#"):
            output.append(_rewrite_tag_uris(line, token, playlist_url, proxy_base_url))
            continue
        absolute_url = urljoin(playlist_url, line)
        if is_ad_segment(pending_duration, absolute_url):
            if output and output[-1].startswith("#EXTINF:"):
                output.pop()
            pending_duration = None
            continue
        new_segments.append(
            PlaylistSegment(index=segment_index, url=absolute_url, duration=pending_duration)
        )
        output.append(f"{proxy_base_url}/seg?token={quote(token)}&i={segment_index}")
        segment_index += 1
        pending_duration = None
    session.segments = new_segments
    return RewrittenPlaylist(text="\n".join(output) + "\n", is_master=False)


def _rewrite_tag_uris(line: str, token: str, playlist_url: str, proxy_base_url: str) -> str:
    def repl(match: re.Match[str]) -> str:
        absolute_url = urljoin(playlist_url, match.group(1))
        return f'URI="{proxy_base_url}/asset?token={quote(token)}&url={quote(absolute_url, safe="")}"'

    return _URI_ATTR_RE.sub(repl, line)
