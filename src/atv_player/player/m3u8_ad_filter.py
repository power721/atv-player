from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx

from atv_player.models import VideoQualityOption
from atv_player.paths import app_cache_dir
from atv_player.player.bluray_iso import BlurayIsoInspector, is_remote_iso_url
from atv_player.proxy.cenc import parse_cenc_media_fragment
from atv_player.proxy.server import LocalHlsProxyServer
from atv_player.proxy.stripper import TS_PACKET_SIZE, repair_segment_bytes
from atv_player.request_headers import normalize_media_request_headers

_AD_MARKERS = ("/adjump/", "/video/adjump/")
_PLAYLIST_TIMEOUT_SECONDS = 10.0
_URI_ATTRIBUTE_RE = re.compile(r'URI="([^"]+)"')
_MAX_NESTED_PLAYLIST_DEPTH = 3
_DISGUISED_MEDIA_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
_PROBE_RANGE_HEADER = "bytes=0-2047"


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
    removed_discontinuity_count = 0
    pending_extinf: str | None = None
    removed_ad_segment = False

    for line in lines:
        if line.startswith("#EXTINF:"):
            pending_extinf = line
            continue
        if line.startswith("#"):
            if line == "#EXT-X-DISCONTINUITY" and removed_ad_segment:
                removed_discontinuity_count += 1
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

    changed = removed_explicit_ad_segment or removed_discontinuity_count > 0
    if changed:
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
    path_name = (parsed.path or "").rstrip("/").rsplit("/", 1)[-1].lower()
    has_m3u8_suffix = ".m3u8" in url.lower()
    has_m3u8_endpoint = path_name == "m3u8"
    if parsed.scheme not in {"http", "https"} or not (has_m3u8_suffix or has_m3u8_endpoint) or not hostname:
        return False
    if hostname == "localhost":
        return True
    try:
        ip_address(hostname)
        return True
    except ValueError:
        return "." in hostname


def _is_disguised_media_url(url: str) -> bool:
    candidate = url.lower().split("#", 1)[0]
    return any(candidate.endswith(ext) or f"{ext}?" in candidate for ext in _DISGUISED_MEDIA_SUFFIXES)


def _is_extensionless_remote_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    path = parsed.path or ""
    if parsed.scheme not in {"http", "https"} or not hostname or not path or path.endswith("/"):
        return False
    segment = path.rsplit("/", 1)[-1]
    return bool(segment) and "." not in segment


def _should_assume_disguised_media_on_probe_failure(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname.endswith("xhscdn.com")


def _is_remote_proxy_candidate_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False
    if _is_remote_m3u8_url(url):
        return True
    if not (_is_disguised_media_url(url) or _is_extensionless_remote_url(url)):
        return False
    if hostname == "localhost":
        return True
    try:
        ip_address(hostname)
        return True
    except ValueError:
        return "." in hostname


def _is_dash_data_uri(url: str) -> bool:
    return url.startswith("data:application/dash+xml;base64,")


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
        proxy_server: LocalHlsProxyServer | None = None,
        cache_dir: Path | None = None,
        get: Callable[..., object] = httpx.get,
        bluray_iso_inspector: BlurayIsoInspector | None = None,
    ) -> None:
        self._cache_dir = cache_dir or (app_cache_dir() / "playlists")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._get = get
        self._proxy_server = proxy_server or LocalHlsProxyServer(get=get)
        self._bluray_iso_inspector = bluray_iso_inspector or BlurayIsoInspector()

    def should_prepare(self, url: str) -> bool:
        if parse_cenc_media_fragment(url) is not None:
            return True
        if is_remote_iso_url(url):
            return True
        if _is_dash_data_uri(url):
            return True
        return _is_remote_proxy_candidate_url(url)

    def prepare(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        dash_video_id: str | None = None,
    ) -> str:
        if not self.should_prepare(url):
            return url
        self._proxy_server.start()
        cenc_media = parse_cenc_media_fragment(url)
        if cenc_media is not None:
            media_url, spade_a = cenc_media
            return self._proxy_server.create_cenc_media_url(
                media_url,
                headers=normalize_media_request_headers(media_url, headers),
                spade_a=spade_a,
            )
        normalized_headers = normalize_media_request_headers(url, headers)
        if is_remote_iso_url(url):
            prepare_playback = getattr(self._bluray_iso_inspector, "prepare_playback", None)
            iso_stream_source: object | None = None
            if callable(prepare_playback):
                playback_plan = prepare_playback(url, normalized_headers)
                playlist_segments = tuple(getattr(playback_plan, "playlist_segments", ()) or ())
                if playlist_segments:
                    return self._proxy_server.create_iso_playlist_url(
                        url,
                        headers=normalized_headers,
                        segments=playlist_segments,
                    )
                selected_stream = playback_plan.stream
                iso_stream_source = playback_plan.source
            else:
                selected_stream = self._bluray_iso_inspector.inspect(url, normalized_headers)
            return self._proxy_server.create_iso_media_url(
                url,
                headers=normalized_headers,
                stream_path=selected_stream.path,
                stream_size=selected_stream.size,
                iso_stream_source=iso_stream_source,
            )
        if _is_dash_data_uri(url):
            if dash_video_id:
                return self._proxy_server.create_dash_url(
                    url,
                    headers=normalized_headers,
                    selected_video_id=dash_video_id,
                )
            return self._proxy_server.create_dash_url(url, headers=normalized_headers)
        if _is_remote_m3u8_url(url):
            return self._proxy_server.create_playlist_url(url, headers=normalized_headers)
        if _is_disguised_media_url(url):
            return self._proxy_server.create_media_url(url, headers=normalized_headers)
        if _is_extensionless_remote_url(url):
            probe_result = self._looks_like_disguised_ts(url, headers=normalized_headers)
            if probe_result is True:
                return self._proxy_server.create_media_url(url, headers=normalized_headers)
            if probe_result is None and _should_assume_disguised_media_on_probe_failure(url):
                return self._proxy_server.create_media_url(url, headers=normalized_headers)
            return url
        return self._proxy_server.create_playlist_url(url, headers=normalized_headers)

    def _looks_like_disguised_ts(self, url: str, headers: dict[str, str]) -> bool | None:
        probe_headers = dict(headers)
        probe_headers["Range"] = _PROBE_RANGE_HEADER
        try:
            response = self._get(
                url,
                headers=probe_headers,
                timeout=_PLAYLIST_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
            response.raise_for_status()
        except Exception:
            return None
        payload = bytes(getattr(response, "content", b"") or b"")
        repaired = repair_segment_bytes(payload)
        if len(repaired) < TS_PACKET_SIZE * 2:
            return False
        return repaired[0] == 0x47 and repaired[TS_PACKET_SIZE] == 0x47

    def close(self) -> None:
        self._proxy_server.close()

    def dash_video_qualities(self, prepared_url: str) -> list[VideoQualityOption]:
        codec_names = {
            "AVC1": "AVC",
            "HEV1": "HEVC",
            "HVC1": "HEVC",
            "AV01": "AV1",
        }
        qualities: list[VideoQualityOption] = []
        for representation in self._proxy_server.dash_video_representations(prepared_url):
            label_parts: list[str] = []
            if representation.height > 0:
                label_parts.append(f"{representation.height}P")
            elif representation.width > 0:
                label_parts.append(f"{representation.width}W")
            codec = representation.codecs.split(".", 1)[0].upper() if representation.codecs else ""
            codec_label = codec_names.get(codec, codec)
            if codec_label:
                label_parts.append(codec_label)
            if representation.bandwidth > 0:
                label_parts.append(f"{representation.bandwidth / 1_000_000:.1f} Mbps")
            qualities.append(
                VideoQualityOption(
                    id=representation.id,
                    label=" ".join(label_parts) or representation.id,
                    width=representation.width,
                    height=representation.height,
                    bandwidth=representation.bandwidth,
                    codecs=representation.codecs,
                )
            )
        return qualities

    def selected_dash_video_quality(self, prepared_url: str) -> str | None:
        return self._proxy_server.selected_dash_video_representation_id(prepared_url)

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
