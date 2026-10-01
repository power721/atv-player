from __future__ import annotations

import base64
from hashlib import sha256
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import errno
import logging
import math
import queue
import socket
import re
import threading
import time
from collections.abc import Callable
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
from atv_player.proxy.ad_filter import MODE_MARKERS
from atv_player.proxy.cenc import CencRangeReader
from atv_player.proxy.m3u8 import rewrite_playlist
from atv_player.proxy.range_proxy import (
    ClientDisconnectError,
    RangeProxyRegistry,
    RangeProxySource,
    RangeProxyTask,
    ensure_probed,
    open_sequential_stream,
    parse_range_header,
    serve_parallel_range,
)
from atv_player.proxy.segment import SegmentProxy
from atv_player.proxy.session import DashRepresentation, ProxySession, ProxySessionRegistry
from atv_player.request_headers import normalize_media_request_headers

logger = logging.getLogger(__name__)

# 客户端(mpv/应用自身)卡死后既不读也不关连接,流式写会永久挂住 handler 线程
# 并泄漏 socket。给本地代理连接统一设置读写超时,超时后放弃该次传输。
_CLIENT_STALL_TIMEOUT_SECONDS = 600.0

_ISO_STREAM_CHUNK_SIZE = 256 * 1024
_DASH_STREAM_CHUNK_SIZE = 256 * 1024
_CENC_STREAM_CHUNK_SIZE = 256 * 1024
_RANGE_PROXY_STREAM_CHUNK_SIZE = 256 * 1024
_DASH_HTTP_CHUNK_SIZE_SCHEME = "urn:atv-player:http-chunk-size"
# 音频上游吞吐门:未锁定的音频候选除连接/响应头检查外,还必须在限时内
# 交出首块字节。B站 PCDN 边缘存在"能连上、响应头正常、体数据断断续续"
# 的劣化形态(2026-10-01 无声事故),头部检查拦不住,粘住锁定后外挂音轨
# 会被饿死(画面正常、无声、无报错)。计时从发起连接开始,连头部阶段的
# 高延迟劣化一起拦。
_DASH_AUDIO_PROBE_BYTES = 128 * 1024
_DASH_AUDIO_PROBE_SECONDS = 2.5
_DASH_AUDIO_PROBE_READ_CHUNK = 16 * 1024
_TLS_PROTOCOL_MISMATCH_MARKERS = (
    "wrong version number",
    "record layer failure",
)


def _is_client_disconnect_error(exc: BaseException) -> bool:
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in {errno.EPIPE, errno.ECONNRESET}
    if isinstance(exc, socket.error):
        return True
    return False


def _is_client_stall_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (socket.timeout, TimeoutError))


def _summarize_range_proxy_url(url: str) -> str:
    parsed = urlparse(url or "")
    if not parsed.netloc:
        return url
    path = parsed.path or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path[:96]}"


class _RangeProxyChunkChannel:
    """并行分片生产者线程 → 响应线程的有界字节通道。

    有界队列提供背压;`first()` 是提交门限——拿到首块之前播放器 socket 未写,
    并行失败可整体降级顺序重发;一旦首块已写出,中途失败只能断流让播放器重试。
    """

    _SENTINEL = None

    def __init__(self, maxsize: int = 8) -> None:
        self._chunks: queue.Queue = queue.Queue(maxsize=maxsize)
        self._failure: Exception | None = None
        self._closed = False

    def put(self, chunk: bytes) -> None:
        while not self._closed:
            try:
                self._chunks.put(chunk, timeout=0.1)
                return
            except queue.Full:
                continue

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        while True:
            try:
                self._chunks.put(self._SENTINEL, timeout=0.1)
                return
            except queue.Full:
                continue

    def fail(self, exc: Exception) -> None:
        self._failure = exc
        self.close()

    def abort(self) -> None:
        self.fail(ClientDisconnectError("client disconnected"))

    @property
    def failure(self) -> Exception | None:
        return self._failure

    def first(self) -> bytes | None:
        item = self._chunks.get()
        return None if item is self._SENTINEL else item

    def iter_after(self):
        """首块已由调用方单独写出,这里只继续吐后续块(勿重复 yield 首块)。"""
        while True:
            item = self._chunks.get()
            if item is self._SENTINEL:
                return
            yield item

    def drain(self) -> None:
        self._closed = True
        while True:
            try:
                self._chunks.get(timeout=0.1)
            except queue.Empty:
                return


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
            set_segmented = any(
                _dash_child_elements(adaptation_set, prefix, child)
                for child in ("SegmentTemplate", "SegmentList")
            )
            for representation in _dash_child_elements(adaptation_set, prefix, "Representation"):
                base_url_element = next(iter(_dash_child_elements(representation, prefix, "BaseURL")), None)
                base_url = unescape((base_url_element.text or "").strip()) if base_url_element is not None else ""
                parsed_representation = _dash_representation_from_element(representation, base_url)
                parsed_representation.segmented = set_segmented or any(
                    _dash_child_elements(representation, prefix, child)
                    for child in ("SegmentTemplate", "SegmentList")
                )
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
    # rewrite 会整表重建 dash_assets;.mpd 重算是幂等的,但音频故障转移粘住的
    # 上游地址必须跨重建保留,否则坏边缘会被换回来。
    sticky_audio_url = (
        session.dash_assets[session.dash_audio_asset_index]
        if session.dash_audio_upstream_locked
        and 0 <= session.dash_audio_asset_index < len(session.dash_assets)
        else ""
    )
    session.dash_assets = []
    session.dash_asset_chunk_sizes = []
    session.dash_video_asset_index = -1
    session.dash_audio_asset_index = -1
    prefix = _dash_namespace_prefix(root)
    namespace = prefix[1:-1] if prefix else ""

    # 选中且非多分段表示的 BaseURL 即完整媒体文件,可走直连分发;按原始 URL 对账。
    selected_rep_base_urls: dict[str, str] = {}
    for role, representations, selected_id in (
        ("video", session.dash_video_representations, session.selected_dash_video_id),
        ("audio", session.dash_audio_representations, session.selected_dash_audio_id),
    ):
        for representation in representations:
            if representation.id != selected_id or representation.segmented:
                continue
            base_url = representation.base_url
            if base_url.startswith(("http://", "https://")) and base_url not in selected_rep_base_urls:
                selected_rep_base_urls[base_url] = role
            break

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
        role = selected_rep_base_urls.get(raw_url)
        if role == "video" and session.dash_video_asset_index < 0:
            session.dash_video_asset_index = asset_index
        elif role == "audio" and session.dash_audio_asset_index < 0:
            session.dash_audio_asset_index = asset_index

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
    if sticky_audio_url and 0 <= session.dash_audio_asset_index < len(session.dash_assets):
        session.dash_assets[session.dash_audio_asset_index] = sticky_audio_url
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


def _dash_audio_probe_iterator(response: Any):
    """吞吐门用的小 chunk 响应体迭代器(探针要按字节到达节奏计时)。"""
    iter_bytes = getattr(response, "iter_bytes")
    try:
        return iter_bytes(chunk_size=_DASH_AUDIO_PROBE_READ_CHUNK)
    except TypeError:
        return iter_bytes()


def _dash_asset_range_starts_at_zero(range_header: str | None) -> bool:
    """该请求是否从文件头读取(新开 demuxer,如 audio-add/audio-reload)。"""
    if not range_header:
        return True
    parsed = _parse_byte_range_header(range_header)
    if parsed is None:
        return False
    return parsed[0] == 0


def _close_context_quietly(context_manager: Any) -> None:
    closer = getattr(context_manager, "__exit__", None)
    if closer is None:
        return
    try:
        closer(None, None, None)
    except Exception:
        pass


def _summarize_upstream_url(url: str) -> str:
    parsed = urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return url
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


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


def _is_tls_protocol_mismatch(exc: httpx.TransportError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _TLS_PROTOCOL_MISMATCH_MARKERS)


def _plain_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    return parsed._replace(scheme="http").geturl()


def _get_playlist_with_plain_http_fallback(
    get: Any,
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    follow_redirects: bool,
) -> tuple[Any, str]:
    try:
        response = get(
            url,
            headers=headers,
            timeout=timeout,
            follow_redirects=follow_redirects,
        )
        return response, _response_effective_url(response, url)
    except httpx.TransportError as exc:
        fallback_url = _plain_http_url(url)
        if not fallback_url or not _is_tls_protocol_mismatch(exc):
            raise
        logger.info(
            "Retry HLS playlist over plain HTTP after TLS protocol mismatch url=%s fallback_url=%s",
            url,
            fallback_url,
            extra={"log_category": "network", "log_source": "app"},
        )
        response = get(
            fallback_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=follow_redirects,
        )
        return response, _response_effective_url(response, fallback_url)


def _response_effective_url(response: Any, fallback_url: str) -> str:
    response_url = getattr(response, "url", None)
    if response_url is None:
        return fallback_url
    normalized_url = str(response_url).strip()
    return normalized_url or fallback_url


class LocalHlsProxyServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 2323,
        get=httpx.get,
        stream=_default_stream,
        segment_prefetch_size: int = 2,
        ad_filter_mode: str = MODE_MARKERS,
    ) -> None:
        self.host = host
        self.port = port
        self._preferred_port = port
        self._get = get
        self._stream = stream
        self._registry = ProxySessionRegistry()
        self._range_registry = RangeProxyRegistry()
        self._dash_audio_failover_lock = threading.Lock()
        self._ad_filter_mode = ad_filter_mode
        self._segment_proxy = SegmentProxy(self._registry, get=get, segment_prefetch_size=segment_prefetch_size)
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

    def set_ad_filter_mode(self, ad_filter_mode: str) -> None:
        self._ad_filter_mode = ad_filter_mode

    def create_playlist_url(self, url: str, headers: dict[str, str] | None = None) -> str:
        token = self._registry.create_session(url, normalize_media_request_headers(url, headers))
        return f"http://{self.host}:{self.port}/m3u/{quote(token, safe='')}"

    def create_media_url(self, url: str, headers: dict[str, str] | None = None) -> str:
        token = self._registry.create_session(url, normalize_media_request_headers(url, headers))
        return f"http://{self.host}:{self.port}/raw?v={quote(token)}"

    def create_cenc_media_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        spade_a: str,
    ) -> str:
        """为整段 CENC 加密的 MP4 建立流式解密会话。"""
        normalized_headers = normalize_media_request_headers(url, headers)
        token = self._registry.create_session(url, normalized_headers)
        session = self._registry.get(token)
        if session is not None:
            session.cenc_reader = CencRangeReader(
                url,
                normalized_headers,
                spade_a,
                get=self._get,
            )
        return f"http://{self.host}:{self.port}/cenc/{quote(token, safe='')}.mp4"

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
            # 提前跑一遍 rewrite:立刻得到 dash_assets 与选中表示的下标,
            # 供直连分发(dash_direct_media_urls)使用;.mpd 请求时会幂等重算。
            _rewrite_dash_manifest(
                session.dash_manifest_payload,
                session,
                f"http://{self.host}:{self.port}",
            )
        return f"http://{self.host}:{self.port}/dash/{quote(token)}.mpd"

    def create_range_proxy_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        drive_type: str = "",
        concurrency: int = 0,
        chunk_size: int = 0,
        sources: list[RangeProxySource] | None = None,
        task_id: str = "",
        file_name: str = "",
        content_type: str = "",
    ) -> str | None:
        """注册客户端多线程 Range 代理任务,返回本地代理 URL。

        返回 None 表示该盘类型未配置并发规则(或不支持并行),调用方应继续用原地址播放。
        """
        resolved_task_id = task_id or sha256(url.encode("utf-8")).hexdigest()[:16]
        task = RangeProxyTask(
            resolved_task_id,
            url,
            headers,
            drive_type=drive_type,
            concurrency=concurrency,
            chunk_size=chunk_size,
            sources=sources,
            content_type=content_type,
            file_name=file_name,
        )
        if not task.enabled:
            return None
        self.start()
        self._range_registry.register(task)
        return f"http://{self.host}:{self.port}/driver/{quote(resolved_task_id, safe='')}"

    def close_range_task(self, task_id: str) -> None:
        self._range_registry.remove(task_id)

    def _range_session(self, path: str) -> RangeProxyTask | None:
        parsed = urlparse(path)
        if not parsed.path.startswith("/driver/"):
            return None
        task_id = unquote(parsed.path[len("/driver/") :])
        return self._range_registry.get(task_id)

    @staticmethod
    def _range_proxy_headers(
        task: RangeProxyTask,
        probe,
        start: int,
        end: int,
        total: int,
        is_partial: bool,
    ) -> list[tuple[str, str]]:
        headers = [
            ("Content-Type", probe.content_type or "application/octet-stream"),
            ("Accept-Ranges", "bytes"),
        ]
        if is_partial and total > 0:
            headers.append(("Content-Range", f"bytes {start}-{end}/{total}"))
        if end >= start:
            headers.append(("Content-Length", str(end - start + 1)))
        if task.file_name:
            # http.server 的 send_header 按 latin-1 严格编码,中文文件名会直接抛异常;
            # 非 ASCII 名走 RFC 5987 filename* 编码(纯 ASCII)。
            safe_name = (
                task.file_name.replace('"', "").replace("\r", "").replace("\n", "")
            )
            try:
                safe_name.encode("ascii")
                headers.append(
                    ("Content-Disposition", f'attachment; filename="{safe_name}"')
                )
            except UnicodeEncodeError:
                encoded = quote(safe_name)
                headers.append(
                    ("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
                )
        return headers

    def _resolve_range_proxy_span(
        self,
        task: RangeProxyTask,
        request_headers: dict[str, str],
    ) -> tuple[object, int, int, int, bool] | tuple[int, bytes, list[tuple[str, str]]]:
        """探测 + Range 解析。返回 ("ok", probe, start, end, total, is_partial) 或 ("error", payload, headers)。"""
        try:
            probe = ensure_probed(task, get=self._get)
        except Exception as exc:
            logger.warning(
                "Range proxy probe failed task=%s url=%s error=%s",
                task.task_id,
                _summarize_range_proxy_url(task.url),
                exc,
                extra={"log_category": "network", "log_source": "app"},
            )
            return 502, f"range proxy probe failed: {exc}".encode("utf-8"), []
        total = probe.total_length
        range_header = request_headers.get("Range") or request_headers.get("range") or ""
        parsed_range = parse_range_header(range_header, total) if range_header else None
        if range_header and parsed_range is None and total > 0:
            return (
                416,
                b"invalid range",
                [("Content-Range", f"bytes */{total}")],
            )
        if parsed_range is not None:
            start, end = parsed_range
            return "ok", probe, start, end, total, True
        if total > 0:
            return "ok", probe, 0, total - 1, total, False
        return "ok", probe, 0, -1, -1, False

    def _send_range_proxy_error(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        payload: bytes,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        handler.send_response(status)
        if extra_headers:
            for key, value in extra_headers:
                handler.send_header(key, value)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        try:
            handler.wfile.write(payload)
        except Exception as exc:
            if not _is_client_disconnect_error(exc):
                raise

    def _stream_range_proxy_response(
        self,
        path: str,
        request_headers: dict[str, str],
        handler: BaseHTTPRequestHandler,
    ) -> bool:
        parsed = urlparse(path)
        if not parsed.path.startswith("/driver/"):
            return False
        self._range_registry.expire()
        task = self._range_session(path)
        if task is None:
            self._send_range_proxy_error(handler, 404, b"missing range proxy task")
            return True
        task.refresh_access()
        task.active_sessions += 1
        try:
            resolved = self._resolve_range_proxy_span(task, request_headers)
            if resolved[0] != "ok":
                _status, payload, extra_headers = resolved
                self._send_range_proxy_error(handler, _status, payload, extra_headers)
                return True
            _tag, probe, start, end, total, is_partial = resolved
            if task.state != "ready_parallel" or end < start or task.should_use_sequential_fallback():
                return self._serve_range_proxy_sequential(handler, task, probe, start, end, total, is_partial)
            return self._serve_range_proxy_parallel(handler, task, probe, start, end, total, is_partial)
        finally:
            task.active_sessions -= 1

    def _serve_range_proxy_parallel(
        self,
        handler: BaseHTTPRequestHandler,
        task: RangeProxyTask,
        probe,
        start: int,
        end: int,
        total: int,
        is_partial: bool,
    ) -> bool:
        channel = _RangeProxyChunkChannel()
        stop_event = threading.Event()

        def produce() -> None:
            try:
                serve_parallel_range(task, start, end, channel.put, get=self._get, stop_event=stop_event)
                channel.close()
            except ClientDisconnectError:
                channel.abort()
            except Exception as exc:  # noqa: BLE001 - 首块给出前可整体降级,之后只能断流
                channel.fail(exc)

        producer = threading.Thread(
            target=produce,
            daemon=True,
            name=f"range-proxy-producer-{task.task_id}",
        )
        producer.start()
        try:
            first = channel.first()
        except BaseException:
            stop_event.set()
            channel.drain()
            raise
        if first is None:
            # 尚未向播放器写出任何字节:并行失败可整体降级为顺序流式重发。
            stop_event.set()
            channel.drain()
            failure = channel.failure
            logger.info(
                "Range proxy parallel fetch failed, fallback to sequential task=%s url=%s failure=%s",
                task.task_id,
                _summarize_range_proxy_url(task.url),
                failure,
                extra={"log_category": "network", "log_source": "app"},
            )
            return self._serve_range_proxy_sequential(handler, task, probe, start, end, total, is_partial)
        status = 206 if is_partial else 200
        handler.send_response(status)
        for key, value in self._range_proxy_headers(task, probe, start, end, total, is_partial):
            handler.send_header(key, value)
        handler.end_headers()
        try:
            handler.wfile.write(first)
            for chunk in channel.iter_after():
                handler.wfile.write(chunk)
        except Exception as exc:
            stop_event.set()
            channel.drain()
            if not _is_client_disconnect_error(exc):
                # 响应头已提交:只能断开连接,绝不能往同一 socket 补写 502
                # (否则一个响应里出现两个状态行/Content-Length,Chromium 报
                # ERR_RESPONSE_HEADERS_MULTIPLE_CONTENT_LENGTH)。
                logger.warning(
                    "Range proxy stream write failed task=%s error=%s",
                    task.task_id,
                    exc,
                    extra={"log_category": "network", "log_source": "app"},
                )
            return True
        stop_event.set()
        channel.drain()
        if channel.failure is not None:
            # 响应已部分写出,无法重新开始;中断连接让播放器重试(后续请求自动降级顺序模式)。
            logger.warning(
                "Range proxy stream interrupted after commit task=%s failure=%s",
                task.task_id,
                channel.failure,
                extra={"log_category": "network", "log_source": "app"},
            )
        return True

    def _serve_range_proxy_sequential(
        self,
        handler: BaseHTTPRequestHandler,
        task: RangeProxyTask,
        probe,
        start: int,
        end: int,
        total: int,
        is_partial: bool,
    ) -> bool:
        stream_end = end if end >= start else None
        try:
            response, _translated_start = open_sequential_stream(task, start, stream_end, stream=self._stream)
        except Exception as exc:
            self._send_range_proxy_error(handler, 502, f"range proxy upstream failed: {exc}".encode("utf-8"))
            return True
        with response:
            try:
                response.raise_for_status()
            except Exception as exc:
                response.close()
                self._send_range_proxy_error(handler, 502, f"range proxy upstream failed: {exc}".encode("utf-8"))
                return True
            upstream_headers = {
                str(name).lower(): str(value)
                for name, value in response.headers.items()
            }
            status = 206 if is_partial or response.status_code == 206 else 200
            handler.send_response(status)
            local_headers = dict(self._range_proxy_headers(task, probe, start, end, total, is_partial))
            if "Content-Length" not in local_headers and "content-length" in upstream_headers:
                local_headers["Content-Length"] = upstream_headers["content-length"]
            for key, value in local_headers.items():
                handler.send_header(key, value)
            handler.end_headers()
            try:
                for chunk in response.iter_bytes(chunk_size=_RANGE_PROXY_STREAM_CHUNK_SIZE):
                    if chunk:
                        handler.wfile.write(chunk)
            except Exception as exc:
                if not _is_client_disconnect_error(exc):
                    logger.warning(
                        "Range proxy sequential stream failed task=%s error=%s",
                        task.task_id,
                        exc,
                        extra={"log_category": "network", "log_source": "app"},
                    )
                # 响应头已提交:断开连接即可,不得补写错误响应。
        return True

    def _send_range_proxy_head_response(
        self,
        path: str,
        request_headers: dict[str, str],
        handler: BaseHTTPRequestHandler,
    ) -> bool:
        if not urlparse(path).path.startswith("/driver/"):
            return False
        task = self._range_session(path)
        if task is None:
            handler.send_response(404)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
        task.refresh_access()
        resolved = self._resolve_range_proxy_span(task, request_headers)
        if resolved[0] != "ok":
            _status, _payload, extra_headers = resolved
            handler.send_response(_status)
            for key, value in extra_headers:
                handler.send_header(key, value)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
        _tag, probe, start, end, total, is_partial = resolved
        handler.send_response(206 if is_partial else 200)
        for key, value in self._range_proxy_headers(task, probe, start, end, total, is_partial):
            handler.send_header(key, value)
        if end < start:
            handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True

    def _dash_session_for_url(self, dash_url: str) -> ProxySession | None:
        parsed = urlparse(dash_url)
        try:
            if parsed.path.startswith("/dash/asset/"):
                token = self._dash_asset_path(parsed.path)[0]
            elif parsed.path.startswith("/dash/"):
                token = self._path_token(parsed.path)
            else:
                token = self._query_token(parse_qs(parsed.query))
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

    def dash_direct_media_urls(self, dash_url: str) -> tuple[str, str]:
        """单文件 DASH(无 SegmentTemplate/SegmentList)的直连地址:(视频, 音频)。

        返回的地址走既有 /dash/asset/ 代理(保留上游 Referer/UA 头与 Range 转发),
        mpv 用 mov demuxer 直接打开即可用 sidx 索引随机 seek,绕开 ffmpeg dashdemux
        对单文件表示 seek 时从头线性读的缺陷。视频地址为空表示该清单不适合直连;
        清单里有音轨但音频地址为空时同样放弃直连(避免无声)。
        """
        session = self._dash_session_for_url(dash_url)
        if session is None:
            return ("", "")
        video_url = self._dash_asset_proxy_url(session, session.dash_video_asset_index)
        audio_url = self._dash_asset_proxy_url(session, session.dash_audio_asset_index)
        if not video_url:
            return ("", "")
        if session.dash_audio_representations and not audio_url:
            return ("", "")
        return video_url, audio_url

    def reset_dash_audio_upstream(self, media_url: str) -> bool:
        """外挂音轨断粮后解锁音频上游粘住锁定,让下一次零起点请求重走候选
        故障转移(含吞吐门)。只清锁、不动 dash_assets:非零起点 Range 续读
        仍会落在同一表示上,字节布局不会错乱;audio-reload 重开 demuxer 从
        零读,才能换到健康表示。"""
        try:
            token, _asset_index = self._dash_asset_path(urlparse(media_url or "").path)
        except (KeyError, ValueError):
            return False
        session = self._registry.get(token)
        if session is None:
            return False
        if session.dash_audio_upstream_locked:
            session.dash_audio_upstream_locked = False
            logger.info(
                "DASH 音频上游解锁(外挂音轨断粮,重挂时按候选+吞吐门转移): %s",
                _summarize_upstream_url(media_url or ""),
                extra={"log_category": "network", "log_source": "app"},
            )
        return True

    def _dash_asset_proxy_url(self, session: ProxySession, asset_index: int) -> str:
        if asset_index < 0 or asset_index >= len(session.dash_assets):
            return ""
        return (
            f"http://{self.host}:{self.port}/dash/asset/"
            f"{quote(session.token)}/{asset_index}.m4s"
        )

    def _dash_asset_upstream_candidates(
        self,
        session: ProxySession,
        asset_index: int,
        range_header: str | None = None,
    ) -> list[str]:
        """asset 上游候选地址。

        后端清单里每个表示各分一个独立的 PCDN 边缘(B站 mcdn 节点),单个边缘会
        整段拒连,而音频表示(不同码率)互为天然备份;音频 asset 未锁定时按清单
        顺序带上其余音频表示的直链。视频不转移:跨表示换地址等于换清晰度/编码。
        非零起点的 Range 是同一 demuxer 的续读,跨表示换地址会字节错位,即使
        未锁定(断粮解锁后)也只回当前地址;零起点/无 Range 的新开 demuxer
        (audio-reload 重挂)才允许跨表示转移。
        """
        current_url = session.dash_assets[asset_index]
        if asset_index != session.dash_audio_asset_index or session.dash_audio_upstream_locked:
            return [current_url]
        if not _dash_asset_range_starts_at_zero(range_header):
            return [current_url]
        candidates: list[str] = []
        for url in [current_url] + [
            representation.base_url for representation in session.dash_audio_representations
        ]:
            if url.startswith(("http://", "https://")) and url not in candidates:
                candidates.append(url)
        return candidates

    def _commit_dash_asset_upstream(self, session: ProxySession, asset_index: int, url: str) -> None:
        with self._dash_audio_failover_lock:
            if url != session.dash_assets[asset_index]:
                logger.warning(
                    "DASH 音频上游故障转移 old=%s new=%s",
                    _summarize_upstream_url(session.dash_assets[asset_index]),
                    _summarize_upstream_url(url),
                    extra={"log_category": "network", "log_source": "app"},
                )
                session.dash_assets[asset_index] = url
            session.dash_audio_upstream_locked = True

    def _open_dash_asset_response(
        self,
        session: ProxySession,
        asset_index: int,
        method: str,
        upstream_headers: dict[str, str],
        *,
        range_header: str | None = None,
    ) -> tuple[Any, Callable[[], None], bytes, Any]:
        """打开 asset 上游响应,返回 (response, closer, 首块字节, 响应体迭代器)。

        音频地址未锁定时按候选顺序故障转移;仅"连接/响应头阶段"的 httpx 错误
        触发转移,首个成功响应立即提交粘住并锁定。所有候选失败抛最后一个异常。
        零起点的音频 GET 还要过吞吐门:限时读完首块字节,交不出就换下一个
        候选——劣化边缘(头部正常、体数据断粮)会穿过头部检查并在锁定后饿死
        外挂音轨。探针已消费的字节经 (首块字节, 迭代器) 无缝续交给调用方
        (httpx 的 iter_bytes 不允许二次迭代)。
        """
        candidates = self._dash_asset_upstream_candidates(session, asset_index, range_header)
        probe_required = (
            method == "GET"
            and asset_index == session.dash_audio_asset_index
            and not session.dash_audio_upstream_locked
            and _dash_asset_range_starts_at_zero(range_header)
        )
        last_exc: Exception | None = None
        for url in candidates:
            started_at = time.monotonic()
            response_cm = self._stream(
                method,
                url,
                headers=upstream_headers,
                timeout=10.0,
                follow_redirects=True,
            )
            try:
                response = response_cm.__enter__()
                response.raise_for_status()
            except httpx.HTTPError as exc:
                _close_context_quietly(response_cm)
                last_exc = exc
                continue
            except Exception:
                _close_context_quietly(response_cm)
                raise
            probed: tuple[bytes, Any] | None = None
            if probe_required:
                probed = self._probe_dash_asset_upstream(response, started_at)
                if probed is None:
                    _close_context_quietly(response_cm)
                    last_exc = httpx.HTTPError(
                        "dash audio upstream first-chunk gate timed out after "
                        f"{_DASH_AUDIO_PROBE_SECONDS:.1f}s"
                    )
                    logger.warning(
                        "DASH 音频上游吞吐门未通过(%.1fs 内未交出 %dKB 首块),换下一候选: %s",
                        _DASH_AUDIO_PROBE_SECONDS,
                        _DASH_AUDIO_PROBE_BYTES // 1024,
                        _summarize_upstream_url(url),
                        extra={"log_category": "network", "log_source": "app"},
                    )
                    continue
            prefix, body_iterator = probed if probed is not None else (b"", None)
            self._commit_dash_asset_upstream(session, asset_index, url)
            return response, lambda: _close_context_quietly(response_cm), prefix, body_iterator
        assert last_exc is not None
        raise last_exc

    def _probe_dash_asset_upstream(self, response: Any, started_at: float) -> tuple[bytes, Any] | None:
        """限时读取上游首块,返回 (首块字节, 已部分消费的响应体迭代器)。

        迭代器必须回传给调用方续用——httpx 的 iter_bytes 只允许迭代一次,
        所以探针用小 chunk 建迭代器,后续流式写直接接着消费它。响应体在
        限时内自然结束(短 Range 尾巴)视为通过;超时未凑够字节返回 None。
        """
        deadline = started_at + _DASH_AUDIO_PROBE_SECONDS
        body_iterator = _dash_audio_probe_iterator(response)
        buffer = bytearray()
        try:
            for chunk in body_iterator:
                if chunk:
                    buffer += chunk
                if len(buffer) >= _DASH_AUDIO_PROBE_BYTES or time.monotonic() >= deadline:
                    break
        except httpx.HTTPError:
            return None
        if len(buffer) < _DASH_AUDIO_PROBE_BYTES and time.monotonic() >= deadline:
            return None
        return bytes(buffer), body_iterator

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
        response = None
        last_exc: Exception | None = None
        for url in self._dash_asset_upstream_candidates(session, asset_index, effective_range_header):
            try:
                response = self._get(
                    url,
                    headers=upstream_headers,
                    timeout=10.0,
                    follow_redirects=True,
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                response = None
                last_exc = exc
                continue
            self._commit_dash_asset_upstream(session, asset_index, url)
            break
        if response is None:
            assert last_exc is not None
            raise last_exc
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
        response, close_upstream, probe_prefix, body_iterator = self._open_dash_asset_response(
            session, asset_index, "GET", upstream_headers, range_header=effective_range_header
        )
        try:
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
                        for chunk in self._dash_asset_response_body(response, body_iterator, probe_prefix):
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
            for chunk in self._dash_asset_response_body(response, body_iterator, probe_prefix):
                if chunk:
                    handler.wfile.write(chunk)
        finally:
            close_upstream()
        return True

    def _dash_asset_response_body(self, response: Any, body_iterator: Any, probe_prefix: bytes):
        """上游响应体字节流:探针前缀先行,再续接探针留下的迭代器。"""
        if probe_prefix:
            yield probe_prefix
        if body_iterator is not None:
            yield from body_iterator
        else:
            yield from _iter_response_bytes(response)

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
        response, close_upstream, _probe_prefix, _body_iterator = self._open_dash_asset_response(
            session, asset_index, "HEAD", upstream_headers, range_header=effective_range_header
        )
        try:
            handler.send_response(int(getattr(response, "status_code", 200) or 200))
            for header_name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                header_value = response.headers.get(header_name)
                if header_value:
                    handler.send_header(header_name, header_value)
            handler.end_headers()
        finally:
            close_upstream()
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

    def _cenc_session(self, path: str) -> tuple[ProxySession | None, str]:
        parsed = urlparse(path)
        if not parsed.path.startswith("/cenc/"):
            return None, ""
        token = unquote(parsed.path[len("/cenc/") :].split("/", 1)[0])
        if token.endswith(".mp4"):
            token = token[: -len(".mp4")]
        return self._registry.get(token), token

    def _stream_cenc_response(
        self,
        path: str,
        request_headers: dict[str, str],
        handler: BaseHTTPRequestHandler,
    ) -> bool:
        if not urlparse(path).path.startswith("/cenc/"):
            return False
        session, _token = self._cenc_session(path)
        if session is None or session.cenc_reader is None:
            payload = b"missing proxy session"
            handler.send_response(404)
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True
        reader: CencRangeReader = session.cenc_reader
        try:
            total_size = reader.ensure_index().total_size
        except Exception as exc:
            payload = f"cenc media error: {exc}".encode("utf-8")
            handler.send_response(502)
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True
        range_header = request_headers.get("Range") or request_headers.get("range") or ""
        parsed_range = _parse_byte_range_header(range_header) if range_header else None
        start = parsed_range[0] if parsed_range is not None else 0
        inclusive_end = (
            total_size - 1
            if parsed_range is None or parsed_range[1] is None
            else min(parsed_range[1], total_size - 1)
        )
        if total_size <= 0 or start < 0 or start > total_size or inclusive_end < start:
            payload = b"invalid cenc range"
            handler.send_response(416)
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Content-Range", f"bytes */{max(total_size, 0)}")
            handler.end_headers()
            handler.wfile.write(payload)
            return True
        status_code = 206 if parsed_range is not None else 200
        content_length = inclusive_end - start + 1
        handler.send_response(status_code)
        handler.send_header("Content-Type", "video/mp4")
        handler.send_header("Content-Length", str(content_length))
        handler.send_header("Accept-Ranges", "bytes")
        if parsed_range is not None:
            handler.send_header("Content-Range", f"bytes {start}-{inclusive_end}/{total_size}")
        handler.end_headers()
        cursor = start
        try:
            while cursor <= inclusive_end:
                chunk_end = min(cursor + _CENC_STREAM_CHUNK_SIZE - 1, inclusive_end)
                chunk = reader.read_range(cursor, chunk_end)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                cursor += len(chunk)
        except Exception as exc:
            if _is_client_disconnect_error(exc):
                return True
            raise
        return True

    def _send_cenc_head_response(
        self,
        path: str,
        request_headers: dict[str, str],
        handler: BaseHTTPRequestHandler,
    ) -> bool:
        if not urlparse(path).path.startswith("/cenc/"):
            return False
        session, _token = self._cenc_session(path)
        if session is None or session.cenc_reader is None:
            payload = b"missing proxy session"
            handler.send_response(404)
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            return True
        reader: CencRangeReader = session.cenc_reader
        try:
            total_size = reader.ensure_index().total_size
        except Exception as exc:
            payload = f"cenc media error: {exc}".encode("utf-8")
            handler.send_response(502)
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            return True
        range_header = request_headers.get("Range") or request_headers.get("range") or ""
        parsed_range = _parse_byte_range_header(range_header) if range_header else None
        start = parsed_range[0] if parsed_range is not None else 0
        inclusive_end = (
            total_size - 1
            if parsed_range is None or parsed_range[1] is None
            else min(parsed_range[1], total_size - 1)
        )
        if total_size <= 0 or start < 0 or start > total_size or inclusive_end < start:
            payload = b"invalid cenc range"
            handler.send_response(416)
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Content-Range", f"bytes */{max(total_size, 0)}")
            handler.end_headers()
            return True
        status_code = 206 if parsed_range is not None else 200
        content_length = inclusive_end - start + 1
        handler.send_response(status_code)
        handler.send_header("Content-Type", "video/mp4")
        handler.send_header("Content-Length", str(content_length))
        handler.send_header("Accept-Ranges", "bytes")
        if parsed_range is not None:
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
                    response, effective_playlist_url = _get_playlist_with_plain_http_fallback(
                        self._get,
                        session.playlist_url,
                        headers=session.headers,
                        timeout=10.0,
                        follow_redirects=True,
                    )
                    response.raise_for_status()
                    session.playlist_url = effective_playlist_url
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
                    ad_filter_mode=self._ad_filter_mode,
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
            timeout = _CLIENT_STALL_TIMEOUT_SECONDS

            def do_GET(self) -> None:
                try:
                    if parent._stream_range_proxy_response(self.path, dict(self.headers.items()), self):
                        return
                    if parent._stream_dash_asset_response(self.path, dict(self.headers.items()), self):
                        return
                    if parent._stream_iso_response(self.path, dict(self.headers.items()), self):
                        return
                    if parent._stream_cenc_response(self.path, dict(self.headers.items()), self):
                        return
                except Exception as exc:
                    if _is_client_stall_timeout(exc):
                        logger.warning(
                            "Proxy client stalled for %.0fs, aborting transfer: %s",
                            _CLIENT_STALL_TIMEOUT_SECONDS,
                            self.path,
                        )
                        return
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
                    if parent._send_range_proxy_head_response(self.path, dict(self.headers.items()), self):
                        return
                    if parent._send_dash_asset_head_response(self.path, dict(self.headers.items()), self):
                        return
                    if parent._send_iso_head_response(self.path, dict(self.headers.items()), self):
                        return
                    if parent._send_cenc_head_response(self.path, dict(self.headers.items()), self):
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
