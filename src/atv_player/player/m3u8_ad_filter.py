from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx

from atv_player.paths import app_cache_dir

_AD_MARKERS = ("/adjump/", "/video/adjump/")
_PLAYLIST_TIMEOUT_SECONDS = 10.0
_URI_ATTRIBUTE_RE = re.compile(r'URI="([^"]+)"')
_MAX_NESTED_PLAYLIST_DEPTH = 3


@dataclass(slots=True, frozen=True)
class PlaylistRewriteResult:
    text: str
    changed: bool
    is_master_playlist: bool = False


def _is_ad_segment(line: str) -> bool:
    return any(marker in line for marker in _AD_MARKERS)


def _is_media_uri(line: str) -> bool:
    return bool(line) and not line.startswith("#")


def _remove_redundant_discontinuities(lines: list[str]) -> tuple[list[str], bool]:
    changed = False
    cleaned: list[str] = []
    for index, line in enumerate(lines):
        if line != "#EXT-X-DISCONTINUITY":
            cleaned.append(line)
            continue
        previous_line = cleaned[-1] if cleaned else ""
        media_ahead = any(_is_media_uri(candidate) for candidate in lines[index + 1 :])
        if _is_media_uri(previous_line) and media_ahead:
            cleaned.append(line)
            continue
        changed = True
    return cleaned, changed


def _absolutize_uri_attributes(line: str, playlist_url: str) -> str:
    return _URI_ATTRIBUTE_RE.sub(
        lambda match: f'URI="{urljoin(playlist_url, match.group(1))}"',
        line,
    )


def rewrite_media_playlist(text: str, playlist_url: str) -> PlaylistRewriteResult:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        return PlaylistRewriteResult(text=text, changed=False, is_master_playlist=True)
    output: list[str] = []
    removed_explicit_ad_segment = False
    pending_extinf: str | None = None
    removed_ad_segment = False

    for line in lines:
        if line.startswith("#EXTINF:"):
            pending_extinf = line
            continue
        if line.startswith("#"):
            if line == "#EXT-X-DISCONTINUITY" and removed_ad_segment:
                changed = True
                removed_ad_segment = False
                continue
            output.append(_absolutize_uri_attributes(line, playlist_url))
            if line != "#EXT-X-DISCONTINUITY":
                removed_ad_segment = False
            continue
        absolute_line = urljoin(playlist_url, line)
        if _is_ad_segment(absolute_line):
            removed_explicit_ad_segment = True
            if output and output[-1] == "#EXT-X-DISCONTINUITY":
                output.pop()
            pending_extinf = None
            removed_ad_segment = True
            continue
        if pending_extinf is not None:
            output.append(pending_extinf)
            pending_extinf = None
        output.append(absolute_line)
        removed_ad_segment = False

    changed = removed_explicit_ad_segment
    if removed_explicit_ad_segment:
        output, removed_discontinuity = _remove_redundant_discontinuities(output)
        changed = changed or removed_discontinuity
    return PlaylistRewriteResult(
        text="\n".join(output) + "\n",
        changed=changed,
        is_master_playlist=False,
    )


def _is_remote_m3u8_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or ".m3u8" not in url.lower() or not hostname:
        return False
    if hostname == "localhost":
        return True
    try:
        ip_address(hostname)
        return True
    except ValueError:
        return "." in hostname


def _resolve_first_variant_url(text: str, playlist_url: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        if index + 1 >= len(lines):
            return ""
        candidate = lines[index + 1]
        if candidate.startswith("#"):
            return ""
        return urljoin(playlist_url, candidate)
    return ""


class M3U8AdFilter:
    def __init__(
        self,
        cache_dir: Path | None = None,
        get: Callable[..., object] = httpx.get,
    ) -> None:
        self._cache_dir = cache_dir or (app_cache_dir() / "playlists")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._get = get

    def should_prepare(self, url: str) -> bool:
        return False
        # return _is_remote_m3u8_url(url)

    def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
        return self._prepare(url, headers=dict(headers or {}), depth=0, visited=set())

    def _prepare(self, url: str, headers: dict[str, str], depth: int, visited: set[str]) -> str:
        if not self.should_prepare(url):
            return url
        if depth > _MAX_NESTED_PLAYLIST_DEPTH or url in visited:
            return url
        visited = set(visited)
        visited.add(url)
        print(url)
        try:
            response = self._get(
                url,
                headers=headers,
                timeout=_PLAYLIST_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
            response.raise_for_status()
        except Exception:
            return url
        playlist_text = str(getattr(response, "text", "") or "")
        if not playlist_text.startswith("#EXTM3U"):
            return url
        rewritten = rewrite_media_playlist(playlist_text, url)
        if rewritten.is_master_playlist:
            variant_url = _resolve_first_variant_url(playlist_text, url)
            if not variant_url:
                return url
            prepared_variant = self._prepare(variant_url, headers=headers, depth=depth + 1, visited=visited)
            if self.should_prepare(prepared_variant):
                return url
            return prepared_variant
        if not rewritten.changed:
            return url
        cache_path = self._cache_dir / f"{sha256(url.encode('utf-8')).hexdigest()}.m3u8"
        cache_path.write_text(rewritten.text, encoding="utf-8")
        return str(cache_path)
