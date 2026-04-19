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
_AD_PATH_MARKERS = ("/ads/", "/ad/", "/commercial/", "/promo/")
_PLAYLIST_TIMEOUT_SECONDS = 10.0
_DATERANGE_DURATION_RE = re.compile(r'DURATION=([0-9]+(?:\.[0-9]+)?)')
_DISCONTINUITY_AD_SCORE_THRESHOLD = 80
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


def _parse_extinf_duration(line: str) -> float:
    payload = line[len("#EXTINF:") :]
    value = payload.split(",", 1)[0].strip()
    try:
        return float(value)
    except ValueError:
        return 0.0


def _parse_scte35_daterange_duration(line: str) -> float | None:
    lowered = line.lower()
    if not line.startswith("#EXT-X-DATERANGE:") or "scte35" not in lowered:
        return None
    match = _DATERANGE_DURATION_RE.search(line)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _block_media_urls(block: list[str]) -> list[str]:
    return [line for line in block if _is_media_uri(line)]


def _block_total_duration(block: list[str]) -> float:
    total = 0.0
    pending_duration = 0.0
    for line in block:
        if line.startswith("#EXTINF:"):
            pending_duration = _parse_extinf_duration(line)
            continue
        if _is_media_uri(line):
            total += pending_duration
            pending_duration = 0.0
    return total


def _block_primary_host(block: list[str]) -> str:
    for url in _block_media_urls(block):
        host = urlparse(url).hostname or ""
        if host:
            return host
    return ""


def _is_duration_ad_like(duration: float) -> bool:
    return any(abs(duration - target) < 0.5 for target in (15.0, 30.0, 60.0))


def _score_discontinuity_block(block: list[str], previous_host: str, next_host: str) -> int:
    urls = _block_media_urls(block)
    if not urls:
        return 0
    score = 0
    block_host = _block_primary_host(block)
    if block_host and ((previous_host and block_host != previous_host) or (next_host and block_host != next_host)):
        score += 40
    if any(marker in url.lower() for url in urls for marker in _AD_PATH_MARKERS):
        score += 30
    if _is_duration_ad_like(_block_total_duration(block)):
        score += 20
    return score


def _remove_scored_discontinuity_ad_blocks(lines: list[str]) -> tuple[list[str], bool]:
    blocks: list[list[str]] = [[]]
    for line in lines:
        if line == "#EXT-X-DISCONTINUITY":
            blocks.append([])
            continue
        blocks[-1].append(line)
    keep_flags = [True] * len(blocks)
    changed = False
    hosts = [_block_primary_host(block) for block in blocks]
    for index, block in enumerate(blocks):
        if index == 0 or index == len(blocks) - 1:
            continue
        previous_host = hosts[index - 1]
        next_host = hosts[index + 1]
        if _score_discontinuity_block(block, previous_host, next_host) >= _DISCONTINUITY_AD_SCORE_THRESHOLD:
            keep_flags[index] = False
            changed = True
    rebuilt: list[str] = []
    for index, block in enumerate(blocks):
        if not keep_flags[index]:
            continue
        rebuilt.extend(block)
        if index < len(blocks) - 1 and keep_flags[index + 1]:
            rebuilt.append("#EXT-X-DISCONTINUITY")
    return rebuilt, changed


def rewrite_media_playlist(text: str, playlist_url: str) -> PlaylistRewriteResult:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        return PlaylistRewriteResult(text=text, changed=False, is_master_playlist=True)
    output: list[str] = []
    changed = False
    pending_extinf: str | None = None
    removed_ad_segment = False
    in_explicit_ad_block = False
    daterange_ad_remaining_seconds: float | None = None

    for line in lines:
        if line.startswith("#EXTINF:"):
            if in_explicit_ad_block:
                changed = True
                pending_extinf = None
                continue
            if daterange_ad_remaining_seconds is not None:
                changed = True
                pending_extinf = line
                continue
            pending_extinf = line
            continue
        if line.startswith("#"):
            daterange_duration = _parse_scte35_daterange_duration(line)
            if daterange_duration is not None:
                changed = True
                daterange_ad_remaining_seconds = daterange_duration
                pending_extinf = None
                continue
            if line.startswith(("#EXT-X-CUE-OUT", "#EXT-X-SCTE35-OUT")):
                changed = True
                in_explicit_ad_block = True
                pending_extinf = None
                continue
            if line.startswith(("#EXT-X-CUE-IN", "#EXT-X-SCTE35-IN")):
                changed = True
                in_explicit_ad_block = False
                removed_ad_segment = False
                continue
            if in_explicit_ad_block:
                changed = True
                continue
            if line == "#EXT-X-DISCONTINUITY" and removed_ad_segment:
                changed = True
                removed_ad_segment = False
                continue
            output.append(line)
            if line != "#EXT-X-DISCONTINUITY":
                removed_ad_segment = False
            continue
        absolute_line = urljoin(playlist_url, line)
        if in_explicit_ad_block:
            changed = True
            pending_extinf = None
            removed_ad_segment = True
            continue
        if daterange_ad_remaining_seconds is not None:
            changed = True
            daterange_ad_remaining_seconds -= _parse_extinf_duration(pending_extinf or "")
            pending_extinf = None
            removed_ad_segment = True
            if daterange_ad_remaining_seconds <= 0:
                daterange_ad_remaining_seconds = None
            continue
        if _is_ad_segment(absolute_line):
            changed = True
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

    output, removed_discontinuity = _remove_redundant_discontinuities(output)
    changed = changed or removed_discontinuity
    output, removed_scored_block = _remove_scored_discontinuity_ad_blocks(output)
    changed = changed or removed_scored_block
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
        return _is_remote_m3u8_url(url)

    def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
        return self._prepare(url, headers=dict(headers or {}), depth=0, visited=set())

    def _prepare(self, url: str, headers: dict[str, str], depth: int, visited: set[str]) -> str:
        if not self.should_prepare(url):
            return url
        if depth > _MAX_NESTED_PLAYLIST_DEPTH or url in visited:
            return url
        visited = set(visited)
        visited.add(url)
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
