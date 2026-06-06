from __future__ import annotations

import base64
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import errno
import logging
import math
import socket
import re
import threading
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx

from atv_player.player.bluray_iso import (
    IsoPlaybackSegment,
    create_iso_stream_range_cache,
    read_iso_stream_range,
    read_iso_stream_range_from_source,
)
from atv_player.proxy.m3u8 import rewrite_playlist
from atv_player.proxy.segment import SegmentProxy
from atv_player.proxy.session import DashRepresentation, ProxySession, ProxySessionRegistry
from atv_player.request_headers import normalize_media_request_headers
from atv_player.telegram_media import TelegramMediaError, TelegramMediaService, parse_telegram_media_uri

logger = logging.getLogger(__name__)

_ISO_STREAM_CHUNK_SIZE = 256 * 1024
_DASH_STREAM_CHUNK_SIZE = 256 * 1024
_DASH_HTTP_CHUNK_SIZE_SCHEME = "urn:atv-player:http-chunk-size"


def _is_client_disconnect_error(exc: BaseException) -> bool:
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in {errno.EPIPE, errno.ECONNRESET}
    if isinstance(exc, socket.error):
        return True
    return False


def _is_dash_data_uri(url: str) -> bool:
    return url.startswith("data:application/dash+xml;base64,")


def _decode_dash_manifest(url: str) -> bytes:
    if not _is_dash_data_uri(url):
        raise ValueError("unsupported dash manifest url")
    _prefix, _separator, payload = url.partition(",")
    cleaned = "".join(payload.split())
    return base64.b64decode(cleaned)


_BASE_URL_RE = re.compile(r"(<BaseURL>)(.*?)(</BaseURL>)", re.DOTALL)
_XML_QUOTE_ESCAPES = {'"': "&quot;", "'": "&apos;"}


def _sanitize_dash_manifest(payload: bytes) -> bytes:
    text = payload.decode("utf-8")

    def replace_base_url(match: re.Match[str]) -> str:
        raw_url = match.group(2)
        normalized_url = escape(
            unescape(raw_url),
            _XML_QUOTE_ESCAPES,
        )
        return f"{match.group(1)}{normalized_url}{match.group(3)}"

    return _BASE_URL_RE.sub(replace_base_url, text).encode("utf-8")


def _dash_namespace_prefix(root: ET.Element) -> str:
    namespace_match = re.match(r"\{([^}]+)\}", root.tag)
    namespace = namespace_match.group(1) if namespace_match else ""
    return f"{{{namespace}}}" if namespace else ""


def _dash_child_elements(parent: ET.Element, prefix: str, local_name: str) -> list[ET.Element]:
    return [child for child in parent if child.tag == f"{prefix}{local_name}"]


def _dash_adaptation_content_type(adaptation_set: ET.Element, prefix: str) -> str:
    content_type = str(adaptation_set.attrib.get("contentType") or "").strip().lower()
    if content_type:
        return content_type
    for component in _dash_child_elements(adaptation_set, prefix, "ContentComponent"):
        content_type = str(component.attrib.get("contentType") or "").strip().lower()
        if content_type:
            return content_type
    representation = next(iter(_dash_child_elements(adaptation_set, prefix, "Representation")), None)
    if representation is None:
        return ""
    mime_type = str(representation.attrib.get("mimeType") or "").strip().lower()
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    return ""


def _dash_representation_from_element(representation: ET.Element, base_url: str) -> DashRepresentation:
    def int_attr(name: str) -> int:
        try:
            return int(str(representation.attrib.get(name) or "0").strip() or "0")
        except ValueError:
            return 0

    return DashRepresentation(
        id=str(representation.attrib.get("id") or "").strip(),
        bandwidth=int_attr("bandwidth"),
        width=int_attr("width"),
        height=int_attr("height"),
        codecs=str(representation.attrib.get("codecs") or "").strip(),
        mime_type=str(representation.attrib.get("mimeType") or "").strip(),
        base_url=base_url,
    )


def _video_representation_sort_key(representation: DashRepresentation) -> tuple[int, int, int]:
    return (representation.height, representation.width, representation.bandwidth)


def _dash_representation_http_chunk_size(representation: ET.Element, prefix: str) -> int:
    for supplemental_property in _dash_child_elements(representation, prefix, "SupplementalProperty"):
        scheme_id_uri = str(supplemental_property.attrib.get("schemeIdUri") or "").strip()
        if scheme_id_uri != _DASH_HTTP_CHUNK_SIZE_SCHEME:
            continue
        try:
            chunk_size = int(str(supplemental_property.attrib.get("value") or "0").strip() or "0")
        except ValueError:
            return 0
        return chunk_size if chunk_size > 0 else 0
    return 0


def _parse_dash_session_metadata(
    payload: bytes,
    session: ProxySession,
    *,
    selected_video_id: str | None = None,
) -> None:
    root = ET.fromstring(payload)
    prefix = _dash_namespace_prefix(root)
    session.dash_video_representations = []
    session.dash_audio_representations = []

    for period in [element for element in root.iter() if element.tag == f"{prefix}Period"]:
        for adaptation_set in _dash_child_elements(period, prefix, "AdaptationSet"):
            content_type = _dash_adaptation_content_type(adaptation_set, prefix)
            if content_type not in {"video", "audio"}:
                continue
            for representation in _dash_child_elements(adaptation_set, prefix, "Representation"):
                base_url_element = next(iter(_dash_child_elements(representation, prefix, "BaseURL")), None)
                base_url = unescape((base_url_element.text or "").strip()) if base_url_element is not None else ""
                parsed_representation = _dash_representation_from_element(representation, base_url)
                if content_type == "video":
                    session.dash_video_representations.append(parsed_representation)
                else:
                    session.dash_audio_representations.append(parsed_representation)

    available_video_ids = {representation.id for representation in session.dash_video_representations}
    requested_video_id = (selected_video_id or "").strip()
    if requested_video_id and requested_video_id in available_video_ids:
        session.selected_dash_video_id = requested_video_id
    elif session.dash_video_representations:
        session.selected_dash_video_id = max(
            session.dash_video_representations,
            key=_video_representation_sort_key,
        ).id
    else:
        session.selected_dash_video_id = ""

    available_audio_ids = {representation.id for representation in session.dash_audio_representations}
    if session.selected_dash_audio_id and session.selected_dash_audio_id in available_audio_ids:
        return
    session.selected_dash_audio_id = (
        session.dash_audio_representations[0].id if session.dash_audio_representations else ""
    )


def _rewrite_dash_manifest(payload: bytes, session: ProxySession, proxy_base_url: str) -> bytes:
    root = ET.fromstring(payload)
    session.dash_assets = []
    session.dash_asset_chunk_sizes = []
    prefix = _dash_namespace_prefix(root)
    namespace = prefix[1:-1] if prefix else ""

    periods = [element for element in root.iter() if element.tag == f"{prefix}Period"]
    for period in periods:
        for adaptation_set in list(_dash_child_elements(period, prefix, "AdaptationSet")):
            content_type = _dash_adaptation_content_type(adaptation_set, prefix)
            representations = _dash_child_elements(adaptation_set, prefix, "Representation")
            if content_type == "video":
                selected_representation_id = session.selected_dash_video_id
            elif content_type == "audio":
                selected_representation_id = session.selected_dash_audio_id
            else:
                continue
            selected_representation = next(
                (
                    representation
                    for representation in representations
                    if str(representation.attrib.get("id") or "").strip() == selected_representation_id
                ),
                None,
            )
            if selected_representation is None:
                period.remove(adaptation_set)
                continue
            for extra_representation in list(representations):
                if extra_representation is not selected_representation:
                    adaptation_set.remove(extra_representation)

    processed_base_urls: set[int] = set()

    def rewrite_base_url(base_url: ET.Element, *, chunk_size: int = 0) -> None:
        raw_url = unescape((base_url.text or "").strip())
        asset_index = len(session.dash_assets)
        session.dash_assets.append(raw_url)
        session.dash_asset_chunk_sizes.append(chunk_size if chunk_size > 0 else 0)
        base_url.text = f"{proxy_base_url}/dash/asset/{quote(session.token)}/{asset_index}.m4s"
        processed_base_urls.add(id(base_url))

    for representation in [element for element in root.iter() if element.tag == f"{prefix}Representation"]:
        chunk_size = _dash_representation_http_chunk_size(representation, prefix)
        for base_url in _dash_child_elements(representation, prefix, "BaseURL"):
            rewrite_base_url(base_url, chunk_size=chunk_size)

    for base_url in [element for element in root.iter() if element.tag == f"{prefix}BaseURL"]:
        if id(base_url) in processed_base_urls:
            continue
        rewrite_base_url(base_url)

    if namespace:
        ET.register_namespace("", namespace)
    return ET.tostring(root, encoding="utf-8").replace(b" />", b"/>")


def _parse_byte_range_header(range_header: str) -> tuple[int, int | None] | None:
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
    if match is None:
        return None
    start = int(match.group(1))
    end_text = match.group(2)
    end = int(end_text) if end_text else None
    return start, end


def _dash_asset_chunk_size(session: ProxySession, asset_index: int) -> int:
    try:
        chunk_size = int(session.dash_asset_chunk_sizes[asset_index])
    except (IndexError, TypeError, ValueError):
        return 0
    return chunk_size if chunk_size > 0 else 0


def _bounded_dash_range_header(range_header: str, *, chunk_size: int) -> str:
    if chunk_size <= 0:
        return range_header
    parsed = _parse_byte_range_header(range_header)
    if parsed is None:
        return range_header
    start, end = parsed
    if end is not None:
        return range_header
    return f"bytes={start}-{start + chunk_size - 1}"


def _iter_response_bytes(response: Any):
    iter_bytes = getattr(response, "iter_bytes")
    try:
        yield from iter_bytes(chunk_size=_DASH_STREAM_CHUNK_SIZE)
    except TypeError:
        yield from iter_bytes()


def _slice_payload_for_byte_range(payload: bytes, range_header: str) -> tuple[bytes, str] | None:
    parsed = _parse_byte_range_header(range_header)
    if parsed is None:
        return None
    start, end = parsed
    if start >= len(payload):
        return b"", f"bytes */{len(payload)}"
    inclusive_end = len(payload) - 1 if end is None else min(end, len(payload) - 1)
    if inclusive_end < start:
        return None
    sliced = payload[start : inclusive_end + 1]
    return sliced, f"bytes {start}-{inclusive_end}/{len(payload)}"


def _default_stream(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    follow_redirects: bool,
) -> Any:
    return httpx.stream(
        method,
        url,
        headers=headers,
        timeout=timeout,
        follow_redirects=follow_redirects,
    )


class LocalHlsProxyServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 2323,
        get=httpx.get,
        stream=_default_stream,
        segment_prefetch_size: int = 2,
        telegram_media_service: TelegramMediaService | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._preferred_port = port
        self._get = get
        self._stream = stream
        self._registry = ProxySessionRegistry()
        self._segment_proxy = SegmentProxy(self._registry, get=get, segment_prefetch_size=segment_prefetch_size)
        self._telegram_media_service = telegram_media_service
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        try:
            self._server = ThreadingHTTPServer((self.host, self._preferred_port), self._handler_type())
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or self._preferred_port == 0:
                raise
            logger.warning(
                "Local HLS proxy port busy, fallback to ephemeral port host=%s port=%s",
                self.host,
                self._preferred_port,
                extra={"log_category": "network", "log_source": "app"},
            )
            self._server = ThreadingHTTPServer((self.host, 0), self._handler_type())
        self.port = int(self._server.server_address[1])
        self._server.proxy_server = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None
        self.port = self._preferred_port

    def set_segment_prefetch_size(self, segment_prefetch_size: int) -> None:
        self._segment_proxy.set_segment_prefetch_size(segment_prefetch_size)

    def set_telegram_media_service(self, service: TelegramMediaService | None) -> None:
        self._telegram_media_service = service

    def create_playlist_url(self, url: str, headers: dict[str, str] | None = None) -> str:
        token = self._registry.create_session(url, normalize_media_request_headers(url, headers))
        return f"http://{self.host}:{self.port}/m3u/{quote(token, safe='')}"

    def create_media_url(self, url: str, headers: dict[str, str] | None = None) -> str:
        token = self._registry.create_session(url, normalize_media_request_headers(url, headers))
        return f"http://{self.host}:{self.port}/raw?v={quote(token)}"

    def create_telegram_media_url(self, url: str) -> str:
        parsed = parse_telegram_media_uri(url)
        if parsed is None:
            raise ValueError("invalid telegram media url")
        chat_id, msg_id = parsed
        return f"http://{self.host}:{self.port}/tg/video/{chat_id}/{msg_id}"

    def create_iso_media_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        stream_path: str,
        stream_size: int,
        iso_stream_source: object | None = None,
    ) -> str:
        token = self._registry.create_session(url, normalize_media_request_headers(url, headers))
        session = self._registry.get(token)
        if session is not None:
            session.iso_stream_path = stream_path
            session.iso_stream_size = stream_size
            session.iso_stream_source = iso_stream_source
            session.iso_stream_range_cache = (
                create_iso_stream_range_cache() if iso_stream_source is not None else None
            )
        return f"http://{self.host}:{self.port}/iso/{quote(token)}{stream_path}"

    def create_iso_playlist_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        segments: list[IsoPlaybackSegment] | tuple[IsoPlaybackSegment, ...],
    ) -> str:
        normalized_headers = normalize_media_request_headers(url, headers)
        normalized_segments = tuple(segments)
        if not normalized_segments:
            raise ValueError("iso playlist requires at least one segment")
        segment_urls = [
            self.create_iso_media_url(
                url,
                headers=normalized_headers,
                stream_path=segment.stream_path,
                stream_size=segment.stream_size,
                iso_stream_source=segment.source,
            )
            for segment in normalized_segments
        ]
        target_duration = max(
            1,
            math.ceil(
                max(
                    float(segment.duration_seconds)
                    for segment in normalized_segments
                )
            ),
        )
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
        ]
        for index, (segment, segment_url) in enumerate(zip(normalized_segments, segment_urls, strict=True)):
            if index > 0:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append(f"#EXTINF:{float(segment.duration_seconds):.3f},")
            lines.append(segment_url)
        lines.append("#EXT-X-ENDLIST")
        playlist_text = "\n".join(lines) + "\n"
        token = self._registry.create_session("", {})
        session = self._registry.get(token)
        if session is not None:
            session.cached_playlist_text = playlist_text
        return f"http://{self.host}:{self.port}/m3u/{quote(token, safe='')}"

    def create_dash_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        selected_video_id: str | None = None,
    ) -> str:
        token = self._registry.create_session(url, normalize_media_request_headers(url, headers))
        session = self._registry.get(token)
        if session is not None:
            session.dash_manifest_payload = _sanitize_dash_manifest(_decode_dash_manifest(url))
            _parse_dash_session_metadata(
                session.dash_manifest_payload,
                session,
                selected_video_id=selected_video_id,
            )
        return f"http://{self.host}:{self.port}/dash/{quote(token)}.mpd"

    def _dash_session_for_url(self, dash_url: str) -> ProxySession | None:
        parsed = urlparse(dash_url)
        try:
            token = self._path_token(parsed.path) if parsed.path.startswith("/dash/") else self._query_token(parse_qs(parsed.query))
        except KeyError:
            return None
        return self._registry.get(token)

    def dash_video_representations(self, dash_url: str) -> list[DashRepresentation]:
        session = self._dash_session_for_url(dash_url)
        if session is None:
            return []
        return list(session.dash_video_representations)

    def selected_dash_video_representation_id(self, dash_url: str) -> str | None:
        session = self._dash_session_for_url(dash_url)
        if session is None or not session.selected_dash_video_id:
            return None
        return session.selected_dash_video_id

    @staticmethod
    def _query_token(query: dict[str, list[str]]) -> str:
        values = query.get("token") or query.get("v")
        if not values:
            raise KeyError("token")
        return values[0]

    @staticmethod
    def _m3u_token(path: str, query: dict[str, list[str]]) -> str:
        if path == "/m3u":
            return LocalHlsProxyServer._query_token(query)
        prefix = "/m3u/"
        if not path.startswith(prefix):
            raise KeyError("token")
        token = unquote(path.removeprefix(prefix))
        if not token:
            raise KeyError("token")
        return token

    @staticmethod
    def _path_token(path: str) -> str:
        if not path.startswith("/dash/") or not path.endswith(".mpd"):
            raise KeyError("token")
        token = path.removeprefix("/dash/").removesuffix(".mpd")
        if not token:
            raise KeyError("token")
        return token

    @staticmethod
    def _dash_asset_path(path: str) -> tuple[str, int]:
        prefix = "/dash/asset/"
        if not path.startswith(prefix) or not path.endswith(".m4s"):
            raise KeyError("token")
        asset_path = path.removeprefix(prefix)
        token, _separator, index_part = asset_path.partition("/")
        if not token or not index_part:
            raise KeyError("token")
        return token, int(index_part.removesuffix(".m4s"))

    @staticmethod
    def _iso_path(path: str) -> tuple[str, str]:
        prefix = "/iso/"
        if not path.startswith(prefix):
            raise KeyError("token")
        asset_path = path.removeprefix(prefix)
        token, _separator, stream_path = asset_path.partition("/")
        if not token or not stream_path:
            raise KeyError("token")
        return token, f"/{stream_path}"

    def _read_iso_stream_range(
        self,
        session: ProxySession,
        stream_path: str,
        request_headers: dict[str, str] | None = None,
    ) -> tuple[bytes, int]:
        range_header = (request_headers or {}).get("Range") or (request_headers or {}).get("range") or ""
        parsed = _parse_byte_range_header(range_header) if range_header else None
        start = parsed[0] if parsed is not None else 0
        end = parsed[1] if parsed is not None else None
        if session.iso_stream_source is not None:
            return read_iso_stream_range_from_source(
                session.playlist_url,
                session.headers,
                session.iso_stream_source,
                start,
                end,
                range_cache=session.iso_stream_range_cache,
                get=self._get,
            )
        return read_iso_stream_range(
            session.playlist_url,
            session.headers,
            stream_path,
            start,
            end,
            get=self._get,
        )

    def _proxy_dash_asset(
        self,
        token: str,
        asset_index: int,
        request_headers: dict[str, str] | None = None,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        session = self._registry.get(token)
        if session is None:
            return 404, [], b"missing proxy session"
        if asset_index < 0 or asset_index >= len(session.dash_assets):
            return 404, [], b"missing dash asset"
        upstream_headers = dict(session.headers)
        range_header = (request_headers or {}).get("Range") or (request_headers or {}).get("range")
        effective_range_header = range_header
        if range_header:
            effective_range_header = _bounded_dash_range_header(
                range_header,
                chunk_size=_dash_asset_chunk_size(session, asset_index),
            )
            upstream_headers["Range"] = effective_range_header
        response = self._get(
            session.dash_assets[asset_index],
            headers=upstream_headers,
            timeout=10.0,
            follow_redirects=True,
        )
        response.raise_for_status()
        status_code = int(getattr(response, "status_code", 200) or 200)
        body = bytes(response.content)
        content_range = response.headers.get("Content-Range")
        if effective_range_header and status_code == 200 and not content_range:
            sliced = _slice_payload_for_byte_range(body, effective_range_header)
            if sliced is not None:
                body, content_range = sliced
                status_code = 206
        headers: list[tuple[str, str]] = []
        content_type = response.headers.get("Content-Type")
        if content_type:
            headers.append(("Content-Type", content_type))
        if content_range:
            headers.append(("Content-Range", content_range))
        accept_ranges = response.headers.get("Accept-Ranges")
        if accept_ranges:
            headers.append(("Accept-Ranges", accept_ranges))
        elif range_header or status_code == 206:
            headers.append(("Accept-Ranges", "bytes"))
        return status_code, headers, body

    def _stream_dash_asset_response(
        self,
        path: str,
        request_headers: dict[str, str],
        handler: BaseHTTPRequestHandler,
    ) -> bool:
        if not (path.startswith("/dash/asset/") and path.endswith(".m4s")):
            return False
        token, asset_index = self._dash_asset_path(urlparse(path).path)
        session = self._registry.get(token)
        if session is None:
            payload = b"missing proxy session"
            handler.send_response(404)
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True
        if asset_index < 0 or asset_index >= len(session.dash_assets):
            payload = b"missing dash asset"
            handler.send_response(404)
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True
        upstream_headers = dict(session.headers)
        range_header = request_headers.get("Range") or request_headers.get("range")
        effective_range_header = range_header
        if range_header:
            effective_range_header = _bounded_dash_range_header(
                range_header,
                chunk_size=_dash_asset_chunk_size(session, asset_index),
            )
            upstream_headers["Range"] = effective_range_header
        with self._stream(
            "GET",
            session.dash_assets[asset_index],
            headers=upstream_headers,
            timeout=10.0,
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            status_code = int(getattr(response, "status_code", 200) or 200)
            content_range = response.headers.get("Content-Range")
            if effective_range_header and status_code == 200 and not content_range:
                parsed_range = _parse_byte_range_header(effective_range_header)
                total_size_text = response.headers.get("Content-Length") or ""
                try:
                    total_size = int(total_size_text)
                except ValueError:
                    total_size = 0
                if parsed_range is not None and total_size > 0:
                    start = parsed_range[0]
                    bounded_end = total_size - 1 if parsed_range[1] is None else min(parsed_range[1], total_size - 1)
                    if 0 <= start < total_size and bounded_end >= start:
                        handler.send_response(206)
                        content_type = response.headers.get("Content-Type")
                        if content_type:
                            handler.send_header("Content-Type", content_type)
                        handler.send_header("Content-Length", str(bounded_end - start + 1))
                        handler.send_header("Content-Range", f"bytes {start}-{bounded_end}/{total_size}")
                        handler.send_header("Accept-Ranges", "bytes")
                        handler.end_headers()
                        cursor = 0
                        for chunk in _iter_response_bytes(response):
                            if not chunk:
                                continue
                            next_cursor = cursor + len(chunk)
                            if next_cursor <= start:
                                cursor = next_cursor
                                continue
                            chunk_start = max(start - cursor, 0)
                            chunk_end = len(chunk) if next_cursor - 1 <= bounded_end else bounded_end - cursor + 1
                            if chunk_start < chunk_end:
                                handler.wfile.write(chunk[chunk_start:chunk_end])
                            cursor = next_cursor
                            if cursor > bounded_end:
                                break
                        return True
            handler.send_response(status_code)
            for header_name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                header_value = response.headers.get(header_name)
                if header_value:
                    handler.send_header(header_name, header_value)
            handler.end_headers()
            for chunk in _iter_response_bytes(response):
                if chunk:
                    handler.wfile.write(chunk)
        return True

    def _send_dash_asset_head_response(
        self,
        path: str,
        request_headers: dict[str, str],
        handler: BaseHTTPRequestHandler,
    ) -> bool:
        if not (path.startswith("/dash/asset/") and path.endswith(".m4s")):
            return False
        token, asset_index = self._dash_asset_path(urlparse(path).path)
        session = self._registry.get(token)
        if session is None:
            payload = b"missing proxy session"
            handler.send_response(404)
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            return True
        if asset_index < 0 or asset_index >= len(session.dash_assets):
            payload = b"missing dash asset"
            handler.send_response(404)
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            return True
        upstream_headers = dict(session.headers)
        range_header = request_headers.get("Range") or request_headers.get("range")
        effective_range_header = range_header
        if range_header:
            effective_range_header = _bounded_dash_range_header(
                range_header,
                chunk_size=_dash_asset_chunk_size(session, asset_index),
            )
            upstream_headers["Range"] = effective_range_header
        with self._stream(
            "HEAD",
            session.dash_assets[asset_index],
            headers=upstream_headers,
            timeout=10.0,
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            handler.send_response(int(getattr(response, "status_code", 200) or 200))
            for header_name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                header_value = response.headers.get(header_name)
                if header_value:
                    handler.send_header(header_name, header_value)
            handler.end_headers()
        return True

    def _stream_iso_response(
        self,
        path: str,
        request_headers: dict[str, str],
        handler: BaseHTTPRequestHandler,
    ) -> bool:
        parsed = urlparse(path)
        if not parsed.path.startswith("/iso/"):
            return False
        token, stream_path = self._iso_path(parsed.path)
        session = self._registry.get(token)
        if session is None:
            payload = b"missing proxy session"
            handler.send_response(404)
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True
        total_size = int(session.iso_stream_size)
        range_header = request_headers.get("Range") or request_headers.get("range") or ""
        parsed_range = _parse_byte_range_header(range_header) if range_header else None
        start = parsed_range[0] if parsed_range is not None else 0
        inclusive_end = (
            total_size - 1
            if parsed_range is None or parsed_range[1] is None
            else min(parsed_range[1], total_size - 1)
        )
        if total_size <= 0 or start < 0 or start > total_size or inclusive_end < start:
            payload = b"invalid iso range"
            handler.send_response(416)
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Content-Range", f"bytes */{max(total_size, 0)}")
            handler.end_headers()
            handler.wfile.write(payload)
            return True
        status_code = 206 if parsed_range is not None else 200
        content_length = inclusive_end - start + 1
        handler.send_response(status_code)
        handler.send_header("Content-Type", "video/MP2T")
        handler.send_header("Content-Length", str(content_length))
        handler.send_header("Accept-Ranges", "bytes")
        if parsed_range is not None:
            handler.send_header("Content-Range", f"bytes {start}-{inclusive_end}/{total_size}")
        handler.end_headers()
        cursor = start
        while cursor <= inclusive_end:
            chunk_end = min(cursor + _ISO_STREAM_CHUNK_SIZE - 1, inclusive_end)
            if session.iso_stream_source is not None:
                chunk, _chunk_total_size = read_iso_stream_range_from_source(
                    session.playlist_url,
                    session.headers,
                    session.iso_stream_source,
                    cursor,
                    chunk_end,
                    range_cache=session.iso_stream_range_cache,
                    get=self._get,
                )
            else:
                chunk, _chunk_total_size = read_iso_stream_range(
                    session.playlist_url,
                    session.headers,
                    session.iso_stream_path or stream_path,
                    cursor,
                    chunk_end,
                    get=self._get,
                )
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except Exception as exc:
                if _is_client_disconnect_error(exc):
                    return True
                raise
            cursor += len(chunk)
        return True

    def _send_iso_head_response(
        self,
        path: str,
        request_headers: dict[str, str],
        handler: BaseHTTPRequestHandler,
    ) -> bool:
        parsed = urlparse(path)
        if not parsed.path.startswith("/iso/"):
            return False
        token, _stream_path = self._iso_path(parsed.path)
        session = self._registry.get(token)
        if session is None:
            payload = b"missing proxy session"
            handler.send_response(404)
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            return True
        total_size = int(session.iso_stream_size)
        range_header = request_headers.get("Range") or request_headers.get("range") or ""
        parsed_range = _parse_byte_range_header(range_header) if range_header else None
        start = parsed_range[0] if parsed_range is not None else 0
        inclusive_end = (
            total_size - 1
            if parsed_range is None or parsed_range[1] is None
            else min(parsed_range[1], total_size - 1)
        )
        if total_size <= 0 or start < 0 or start > total_size or inclusive_end < start:
            payload = b"invalid iso range"
            handler.send_response(416)
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Content-Range", f"bytes */{max(total_size, 0)}")
            handler.end_headers()
            return True
        status_code = 206 if parsed_range is not None else 200
        content_length = inclusive_end - start + 1
        handler.send_response(status_code)
        handler.send_header("Content-Type", "video/MP2T")
        handler.send_header("Content-Length", str(content_length))
        handler.send_header("Accept-Ranges", "bytes")
        if parsed_range is not None:
            handler.send_header("Content-Range", f"bytes {start}-{inclusive_end}/{total_size}")
        handler.end_headers()
        return True

    @staticmethod
    def _telegram_video_path(path: str) -> tuple[int, int]:
        prefix = "/tg/video/"
        if not path.startswith(prefix):
            raise KeyError("telegram media")
        payload = path.removeprefix(prefix)
        chat_id_text, separator, msg_id_text = payload.partition("/")
        if not separator or not chat_id_text or not msg_id_text:
            raise KeyError("telegram media")
        return int(chat_id_text), int(msg_id_text)

    def _telegram_media_response_metadata(
        self,
        path: str,
        request_headers: dict[str, str],
    ) -> tuple[int, int, int, int, int, str]:
        parsed = urlparse(path)
        if not parsed.path.startswith("/tg/video/"):
            raise KeyError("telegram media")
        if self._telegram_media_service is None:
            raise TelegramMediaError("telegram media service is not configured")
        chat_id, msg_id = self._telegram_video_path(parsed.path)
        media = self._telegram_media_service.get_media(chat_id, msg_id)
        if media is None:
            raise TelegramMediaError("telegram media not found")
        total_size = int(media.size)
        range_header = request_headers.get("Range") or request_headers.get("range") or ""
        parsed_range = _parse_byte_range_header(range_header) if range_header else None
        start = parsed_range[0] if parsed_range is not None else 0
        inclusive_end = (
            total_size - 1
            if parsed_range is None or parsed_range[1] is None
            else min(parsed_range[1], total_size - 1)
        )
        if total_size <= 0 or start < 0 or start >= total_size or inclusive_end < start:
            return chat_id, msg_id, total_size, start, inclusive_end, media.mime_type or "application/octet-stream"
        return chat_id, msg_id, total_size, start, inclusive_end, media.mime_type or "application/octet-stream"

    def _send_invalid_telegram_range(
        self,
        total_size: int,
        handler: BaseHTTPRequestHandler,
        *,
        write_body: bool,
    ) -> bool:
        payload = b"invalid telegram media range"
        handler.send_response(416)
        handler.send_header("Content-Length", str(len(payload) if write_body else 0))
        handler.send_header("Content-Range", f"bytes */{max(total_size, 0)}")
        handler.end_headers()
        if write_body:
            handler.wfile.write(payload)
        return True

    def _stream_telegram_media_response(
        self,
        path: str,
        request_headers: dict[str, str],
        handler: BaseHTTPRequestHandler,
    ) -> bool:
        parsed = urlparse(path)
        if not parsed.path.startswith("/tg/video/"):
            return False
        chat_id, msg_id, total_size, start, inclusive_end, content_type = self._telegram_media_response_metadata(
            path,
            request_headers,
        )
        if total_size <= 0 or start < 0 or start >= total_size or inclusive_end < start:
            return self._send_invalid_telegram_range(total_size, handler, write_body=True)
        range_header = request_headers.get("Range") or request_headers.get("range")
        status_code = 206 if range_header else 200
        content_length = inclusive_end - start + 1
        logger.info(
            "Telegram media proxy GET chat_id=%s msg_id=%s range=%s status=%s content_range=%s-%s/%s length=%s",
            chat_id,
            msg_id,
            range_header or "",
            status_code,
            start,
            inclusive_end,
            total_size,
            content_length,
            extra={"log_category": "network", "log_source": "telegram"},
        )
        handler.send_response(status_code)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(content_length))
        handler.send_header("Accept-Ranges", "bytes")
        if range_header:
            handler.send_header("Content-Range", f"bytes {start}-{inclusive_end}/{total_size}")
        handler.end_headers()
        assert self._telegram_media_service is not None
        for chunk in self._telegram_media_service.iter_media_bytes(
            chat_id,
            msg_id,
            offset=start,
            limit=content_length,
        ):
            if not chunk:
                continue
            try:
                handler.wfile.write(chunk)
                handler.wfile.flush()
            except Exception as exc:
                if _is_client_disconnect_error(exc):
                    return True
                raise
        return True

    def _send_telegram_media_head_response(
        self,
        path: str,
        request_headers: dict[str, str],
        handler: BaseHTTPRequestHandler,
    ) -> bool:
        parsed = urlparse(path)
        if not parsed.path.startswith("/tg/video/"):
            return False
        _chat_id, _msg_id, total_size, start, inclusive_end, content_type = self._telegram_media_response_metadata(
            path,
            request_headers,
        )
        if total_size <= 0 or start < 0 or start >= total_size or inclusive_end < start:
            return self._send_invalid_telegram_range(total_size, handler, write_body=False)
        range_header = request_headers.get("Range") or request_headers.get("range")
        status_code = 206 if range_header else 200
        content_length = inclusive_end - start + 1
        logger.info(
            "Telegram media proxy HEAD chat_id=%s msg_id=%s range=%s status=%s content_range=%s-%s/%s length=%s",
            _chat_id,
            _msg_id,
            range_header or "",
            status_code,
            start,
            inclusive_end,
            total_size,
            content_length,
            extra={"log_category": "network", "log_source": "telegram"},
        )
        handler.send_response(status_code)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(content_length))
        handler.send_header("Accept-Ranges", "bytes")
        if range_header:
            handler.send_header("Content-Range", f"bytes {start}-{inclusive_end}/{total_size}")
        handler.end_headers()
        return True

    def handle_request(
        self,
        method: str,
        path: str,
        request_headers: dict[str, str] | None = None,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        parsed = urlparse(path)
        query = parse_qs(parsed.query)
        try:
            if method != "GET":
                return 405, [], b"method not allowed"
            if parsed.path == "/m3u" or parsed.path.startswith("/m3u/"):
                token = self._m3u_token(parsed.path, query)
                session = self._registry.get(token)
                if session is None:
                    return 404, [], b"missing proxy session"
                if not session.playlist_url and session.cached_playlist_text is not None:
                    return 200, [("Content-Type", "application/vnd.apple.mpegurl")], session.cached_playlist_text.encode("utf-8")
                try:
                    response = self._get(
                        session.playlist_url,
                        headers=session.headers,
                        timeout=10.0,
                        follow_redirects=True,
                    )
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    if exc.response is not None and exc.response.status_code == 403:
                        if session.cached_playlist_text is not None:
                            return (
                                200,
                                [("Content-Type", "application/vnd.apple.mpegurl")],
                                session.cached_playlist_text.encode("utf-8"),
                            )
                        self._registry.delete(token)
                    raise
                rewritten = rewrite_playlist(
                    token=token,
                    playlist_url=session.playlist_url,
                    content=response.text,
                    session_registry=self._registry,
                    proxy_base_url=f"http://{self.host}:{self.port}",
                )
                session.cached_playlist_text = rewritten.text
                return 200, [("Content-Type", "application/vnd.apple.mpegurl")], rewritten.text.encode("utf-8")
            if parsed.path == "/seg":
                token = self._query_token(query)
                session = self._registry.get(token)
                if session is None:
                    return 404, [], b"missing proxy session"
                index = int(query["i"][0])
                payload = self._segment_proxy.fetch_segment(token, index)
                return 200, [("Content-Type", "video/MP2T")], payload
            if parsed.path == "/asset":
                token = self._query_token(query)
                session = self._registry.get(token)
                if session is None:
                    return 404, [], b"missing proxy session"
                asset_url = query["url"][0]
                payload = self._segment_proxy.fetch_asset(token, asset_url)
                return 200, [], payload
            if parsed.path == "/raw":
                token = self._query_token(query)
                session = self._registry.get(token)
                if session is None:
                    return 404, [], b"missing proxy session"
                payload = self._segment_proxy.fetch_media(token)
                return 200, [("Content-Type", "video/MP2T")], payload
            if parsed.path.startswith("/iso/"):
                token, stream_path = self._iso_path(parsed.path)
                session = self._registry.get(token)
                if session is None:
                    return 404, [], b"missing proxy session"
                payload, total_size = self._read_iso_stream_range(
                    session,
                    session.iso_stream_path or stream_path,
                    request_headers=request_headers,
                )
                range_header = (request_headers or {}).get("Range") or (request_headers or {}).get("range")
                headers = [("Content-Type", "video/MP2T"), ("Accept-Ranges", "bytes")]
                if range_header:
                    parsed_range = _parse_byte_range_header(range_header)
                    if parsed_range is not None:
                        start = parsed_range[0]
                        end = start + len(payload) - 1 if payload else start - 1
                        headers.append(("Content-Range", f"bytes {start}-{end}/{total_size}"))
                        return 206, headers, payload
                return 200, headers, payload
            if parsed.path.startswith("/dash/asset/") and parsed.path.endswith(".m4s"):
                token, asset_index = self._dash_asset_path(parsed.path)
                return self._proxy_dash_asset(token, asset_index, request_headers=request_headers)
            if parsed.path == "/mpd" or (parsed.path.startswith("/dash/") and parsed.path.endswith(".mpd")):
                token = self._path_token(parsed.path) if parsed.path != "/mpd" else self._query_token(query)
                session = self._registry.get(token)
                if session is None:
                    return 404, [], b"missing proxy session"
                proxy_base_url = f"http://{self.host}:{self.port}"
                payload = session.dash_manifest_payload
                if payload is None:
                    payload = _sanitize_dash_manifest(_decode_dash_manifest(session.playlist_url))
                    session.dash_manifest_payload = payload
                if not session.dash_video_representations and not session.dash_audio_representations:
                    _parse_dash_session_metadata(payload, session, selected_video_id=session.selected_dash_video_id or None)
                payload = _rewrite_dash_manifest(
                    payload,
                    session,
                    proxy_base_url,
                )
                return 200, [("Content-Type", "application/dash+xml")], payload
        except Exception as exc:
            return 502, [], str(exc).encode("utf-8")
        return 404, [], b"not found"

    def _handler_type(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                try:
                    if parent._stream_telegram_media_response(self.path, dict(self.headers.items()), self):
                        return
                    if parent._stream_dash_asset_response(self.path, dict(self.headers.items()), self):
                        return
                    if parent._stream_iso_response(self.path, dict(self.headers.items()), self):
                        return
                except Exception as exc:
                    if _is_client_disconnect_error(exc):
                        return
                    payload = str(exc).encode("utf-8")
                    self.send_response(502)
                    self.send_header("Content-Length", str(len(payload)))
                    try:
                        self.end_headers()
                        self.wfile.write(payload)
                    except Exception as write_exc:
                        if _is_client_disconnect_error(write_exc):
                            return
                        raise
                    return
                status, headers, payload = parent.handle_request("GET", self.path, dict(self.headers.items()))
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                try:
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as exc:
                    if _is_client_disconnect_error(exc):
                        return
                    raise

            def do_HEAD(self) -> None:
                try:
                    if parent._send_telegram_media_head_response(self.path, dict(self.headers.items()), self):
                        return
                    if parent._send_dash_asset_head_response(self.path, dict(self.headers.items()), self):
                        return
                    if parent._send_iso_head_response(self.path, dict(self.headers.items()), self):
                        return
                except Exception as exc:
                    if _is_client_disconnect_error(exc):
                        return
                    payload = str(exc).encode("utf-8")
                    self.send_response(502)
                    self.send_header("Content-Length", str(len(payload)))
                    try:
                        self.end_headers()
                    except Exception as write_exc:
                        if _is_client_disconnect_error(write_exc):
                            return
                        raise
                    return
                status, headers, payload = parent.handle_request("HEAD", self.path, dict(self.headers.items()))
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                try:
                    self.end_headers()
                except Exception as exc:
                    if _is_client_disconnect_error(exc):
                        return
                    raise

            def log_message(self, format: str, *args) -> None:
                return None

        return Handler
