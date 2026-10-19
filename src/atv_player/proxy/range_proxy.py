"""客户端多线程 Range 代理(网盘播放加速)。

移植自 CatVodTVSpider 的 VideoStreamProxy(NanoHTTPD 实现):播放器向本地代理发起
普通 Range 请求,代理按盘类型并发数把请求范围切成固定分片,多线程并行拉取上游直链、
按序回写,并带预取窗口/分片重试/顺序降级/多账号分片源轮询。上游不支持 Range 时退化为
流式透传。探测到的混淆 Matroska(PNG 头 + 偏移 8 的 EBML 头)自动跳过前 8 字节。
"""

from __future__ import annotations

import errno
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

TASK_TTL_SECONDS = 30 * 60
DEFAULT_PREFETCH_SEGMENTS = 20
DEFAULT_SEGMENT_RETRY_COUNT = 5
DEFAULT_CHUNK_SIZE = 256 * 1024
# 多账号分片总并发上限:N 源 × 单源并发可能很大,封顶避免压垮设备/网络。
MULTI_SOURCE_MAX_CONCURRENCY = 64
SEQUENTIAL_STREAM_CHUNK_SIZE = 256 * 1024
PROBE_RANGE_HEADER = "bytes=0-2047"
OBFUSCATED_MATROSKA_SKIP = 8
LARGE_FIRST_CHUNK_TRIGGER = 1024 * 1024
LARGE_FIRST_CHUNK_CAP = 25 * 1024 * 1024
_COMPLETION_POLL_SECONDS = 0.02
_UPSTREAM_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# 各盘类型 → (并发数, 分片大小),与 VideoStreamProxy 各盘常量一致;未列出的盘类型不启用。
DRIVE_PROXY_RULES: dict[str, tuple[int, int]] = {
    "ali": (20, 1024 * 1024),
    "quark": (20, 1024 * 1024),
    "uc": (10, 256 * 1024),
    "pan115": (2, 1024 * 1024),
    "pan123": (4, 256 * 1024),
    "pan139": (4, 256 * 1024),
    "baidu": (5, 2 * 1024 * 1024),
}

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_EBML_HEADER = b"\x1a\x45\xdf\xa3"

_BACKEND_DRIVE_TYPES = {
    "ALI": "ali",
    "QUARK": "quark",
    "UC": "uc",
    "PAN115": "pan115",
    "PAN123": "pan123",
    "PAN139": "pan139",
    "BAIDU": "baidu",
}


class ClientDisconnectError(Exception):
    """播放器断开了本地连接,停止回写并放弃在途分片。"""


class UpstreamFetchError(Exception):
    """分片重试后仍失败,触发并行→顺序降级。"""


class StreamCommittedError(Exception):
    """并行回写中途失败:响应已部分写出,无法重新开始,只能中断连接。"""


def normalize_drive_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return _BACKEND_DRIVE_TYPES.get(normalized, normalized)


def parse_drive_type_from_url(url: str) -> str:
    hostname = (urlparse(url or "").hostname or "").lower()
    for domain, drive_type in (
        ("alipan.com", "ali"),
        ("aliyundrive.com", "ali"),
        ("quark.cn", "quark"),
        ("uc.cn", "uc"),
        ("115cdn.net", "pan115"),
        ("115cdn.com", "pan115"),
        ("115.com", "pan115"),
        ("anxia.com", "pan115"),
        ("123pan.com", "pan123"),
        ("123pan.cn", "pan123"),
        ("139.com", "pan139"),
        ("baidu.com", "baidu"),
        ("xunlei.com", "thunder"),
    ):
        if hostname == domain or hostname.endswith(f".{domain}"):
            return drive_type
    return ""


def resolve_proxy_rule(drive_type: str) -> tuple[int, int]:
    """盘类型 → (并发数, 分片字节);未知类型返回 (0, 0) 表示不启用本地并行代理。"""
    return DRIVE_PROXY_RULES.get(normalize_drive_type(drive_type), (0, 0))


def parse_range_header(range_header: str, total_length: int) -> tuple[int, int] | None:
    """解析 Range 为闭区间 (start, end);不可满足/开区间无总长时返回 None。"""
    if not range_header or not range_header.startswith("bytes="):
        return None
    spec = range_header[len("bytes=") :].strip()
    dash = spec.find("-")
    if dash < 0:
        return None
    start_text = spec[:dash].strip()
    end_text = spec[dash + 1 :].strip()
    try:
        if not start_text:
            if not end_text or total_length <= 0:
                return None
            suffix = int(end_text)
            if suffix <= 0:
                return None
            return max(0, total_length - suffix), total_length - 1
        start = int(start_text)
        if start < 0:
            return None
        if not end_text:
            if total_length <= 0:
                return None
            end = total_length - 1
        else:
            end = int(end_text)
            if end < start:
                return None
        if total_length > 0:
            if start >= total_length:
                return None
            end = min(end, total_length - 1)
        return start, end
    except ValueError:
        return None


def compute_initial_segment_size(total_length: int, chunk_size: int) -> int:
    normalized = max(1, chunk_size)
    if normalized > LARGE_FIRST_CHUNK_TRIGGER:
        return min(normalized, LARGE_FIRST_CHUNK_CAP)
    if total_length >= 2 * 1024 * 1024 * 1024:
        quick_start = 256 * 1024
    elif total_length >= 512 * 1024 * 1024:
        quick_start = 128 * 1024
    elif total_length >= 128 * 1024 * 1024:
        quick_start = 64 * 1024
    else:
        quick_start = 32 * 1024
    return min(quick_start, normalized)


def create_segments(
    range_start: int, range_end: int, chunk_size: int, total_length: int
) -> list[tuple[int, int]]:
    """把请求范围切成闭区间分片;首片用快速启动尺寸降低起播延迟。"""
    segments: list[tuple[int, int]] = []
    first_size = min(
        compute_initial_segment_size(total_length, chunk_size),
        range_end - range_start + 1,
    )
    first_end = min(range_start + first_size - 1, range_end)
    segments.append((range_start, first_end))
    start = first_end + 1
    while start <= range_end:
        end = min(start + chunk_size - 1, range_end)
        segments.append((start, end))
        start = end + 1
    return segments


def sanitize_upstream_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """剥头值首尾空白:httpx/h11 严格拒绝,原样发送会抛 Illegal header value。

    后端 UC/夸克直链故意下发「URL+"\\ "」作 Referer 绕 checkplay 回调(Java/okHttp
    可原样发送)。剥掉尾部空白、保留反斜杠,绕过语义不变(实测 UC CDN 对 URL+"\\"
    返回 206,精确 URL 或空 Referer 均 403)。
    """
    sanitized: dict[str, str] = {}
    for key, value in (headers or {}).items():
        name = str(key).strip()
        if not name:
            continue
        sanitized[name] = str(value).strip()
    return sanitized


@dataclass(slots=True)
class RangeProxySource:
    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RangeProxyProbe:
    supports_range: bool
    total_length: int
    content_type: str
    stream_offset: int = 0
    response_headers: dict[str, str] = field(default_factory=dict)


def _detect_obfuscated_matroska(payload: bytes) -> bool:
    if len(payload) < OBFUSCATED_MATROSKA_SKIP + len(_EBML_HEADER):
        return False
    return (
        payload[: len(_PNG_SIGNATURE)] == _PNG_SIGNATURE
        and payload[
            OBFUSCATED_MATROSKA_SKIP : OBFUSCATED_MATROSKA_SKIP + len(_EBML_HEADER)
        ]
        == _EBML_HEADER
    )


class RangeProxyTask:
    """一条本地并行代理任务:上游直链(+header)+按盘类型的并发/分片参数。"""

    def __init__(
        self,
        task_id: str,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        drive_type: str = "",
        concurrency: int = 0,
        chunk_size: int = 0,
        sources: list[RangeProxySource] | None = None,
        content_type: str = "",
        file_name: str = "",
    ) -> None:
        self.task_id = task_id
        self.url = url
        self.headers = sanitize_upstream_headers(headers)
        self.drive_type = normalize_drive_type(drive_type)
        default_concurrency, default_chunk_size = resolve_proxy_rule(self.drive_type)
        if self.drive_type and not default_concurrency:
            # 已知盘类型但未配置并发规则(如 thunder/189):按单线程顺序代理处理。
            default_concurrency, default_chunk_size = 1, DEFAULT_CHUNK_SIZE
        self.concurrency = concurrency if concurrency > 0 else default_concurrency
        self.chunk_size = chunk_size if chunk_size > 0 else default_chunk_size
        self.sources = list(sources) if sources and len(sources) > 1 else None
        if self.sources:
            self.sources = [
                RangeProxySource(source.url, sanitize_upstream_headers(source.headers))
                for source in self.sources
            ]
        if self.sources and self.concurrency > 1:
            # 多账号分片:总并发按源数放大并封顶(与 VideoStreamProxy 一致)。
            self.concurrency = min(
                self.concurrency * len(self.sources), MULTI_SOURCE_MAX_CONCURRENCY
            )
        self.content_type = content_type
        self.file_name = file_name
        self.probe: RangeProxyProbe | None = None
        self.state = "probing"
        self.last_access = time.monotonic()
        self.expire_at = self.last_access + TASK_TTL_SECONDS
        self.parallel_failures = 0
        self.downgraded_at = 0.0
        self.active_sessions = 0

    @property
    def enabled(self) -> bool:
        return self.concurrency > 1

    def refresh_access(self) -> None:
        self.last_access = time.monotonic()
        self.expire_at = self.last_access + TASK_TTL_SECONDS

    def source_for_segment(self, segment_index: int, attempt: int) -> RangeProxySource:
        if not self.sources:
            return RangeProxySource(self.url, self.headers)
        return self.sources[(segment_index + attempt) % len(self.sources)]

    def should_use_sequential_fallback(self, now: float | None = None) -> bool:
        if self.parallel_failures < 3:
            return False
        current = time.monotonic() if now is None else now
        if self.downgraded_at > 0 and current - self.downgraded_at > 5 * 60:
            self.parallel_failures = 0
            self.downgraded_at = 0.0
            return False
        return True

    def record_parallel_failure(self) -> None:
        self.parallel_failures += 1
        if self.parallel_failures >= 3 and self.downgraded_at == 0.0:
            self.downgraded_at = time.monotonic()
            if self.state == "ready_parallel":
                self.state = "ready_sequential"


class RangeProxyRegistry:
    """任务表:注册(替换同 id 旧任务)、查询与 TTL 清理。"""

    def __init__(self, time_source=time.monotonic) -> None:
        self._tasks: dict[str, RangeProxyTask] = {}
        self._lock = threading.Lock()
        self._time_source = time_source

    def register(self, task: RangeProxyTask) -> RangeProxyTask:
        with self._lock:
            existing = self._tasks.pop(task.task_id, None)
            if existing is not None:
                existing.state = "expired"
            self._tasks[task.task_id] = task
            return task

    def get(self, task_id: str) -> RangeProxyTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def remove(self, task_id: str) -> RangeProxyTask | None:
        with self._lock:
            task = self._tasks.pop(task_id, None)
        if task is not None:
            task.state = "expired"
        return task

    def expire(self) -> None:
        now = self._time_source()
        with self._lock:
            for task_id in [
                task_id
                for task_id, task in self._tasks.items()
                if task.active_sessions <= 0 and task.expire_at <= now
            ]:
                task = self._tasks.pop(task_id)
                task.state = "expired"


def probe_task(task: RangeProxyTask, get=httpx.get) -> RangeProxyProbe:
    headers = dict(task.headers)
    headers.pop("Range", None)
    headers["Range"] = PROBE_RANGE_HEADER
    response = get(
        task.url, headers=headers, timeout=_UPSTREAM_TIMEOUT, follow_redirects=True
    )
    response.raise_for_status()
    response_headers = {
        str(name).lower(): str(value)
        for name, value in getattr(response, "headers", {}).items()
    }
    default_content_type = task.content_type or "application/octet-stream"
    upstream_content_type = response_headers.get("content-type", "")
    content_type = (
        upstream_content_type
        if upstream_content_type and upstream_content_type != "application/octet-stream"
        else default_content_type
    )
    probe = RangeProxyProbe(
        supports_range=False,
        total_length=-1,
        content_type=content_type,
        response_headers=response_headers,
    )
    content_range = response_headers.get("content-range", "")
    if content_range.startswith("bytes") and "/" in content_range:
        probe.supports_range = True
        try:
            probe.total_length = int(content_range.rsplit("/", 1)[1].strip())
        except ValueError:
            probe.total_length = -1
    elif "content-length" in response_headers:
        try:
            probe.total_length = int(response_headers["content-length"])
        except ValueError:
            probe.total_length = -1
    payload = bytes(getattr(response, "content", b"") or b"")
    if (
        _detect_obfuscated_matroska(payload)
        and probe.total_length > OBFUSCATED_MATROSKA_SKIP
    ):
        probe.stream_offset = OBFUSCATED_MATROSKA_SKIP
        probe.total_length -= OBFUSCATED_MATROSKA_SKIP
        probe.content_type = "video/x-matroska"
    task.probe = probe
    task.state = (
        "ready_parallel"
        if probe.supports_range and probe.total_length > 0
        else "ready_sequential"
    )
    return probe


def ensure_probed(task: RangeProxyTask, get=httpx.get) -> RangeProxyProbe:
    if task.probe is not None:
        return task.probe
    return probe_task(task, get)


def _fetch_segment(
    task: RangeProxyTask,
    segment_index: int,
    start: int,
    end: int,
    *,
    get=httpx.get,
    stop_event: threading.Event | None = None,
    retry_count: int = DEFAULT_SEGMENT_RETRY_COUNT,
) -> bytes:
    """轮询多账号源拉取一个分片,要求上游 206;重试耗尽抛 UpstreamFetchError。"""
    offset = task.probe.stream_offset if task.probe else 0
    expected_length = end - start + 1
    last_error: Exception | None = None
    for attempt in range(max(1, retry_count)):
        if stop_event is not None and stop_event.is_set():
            raise UpstreamFetchError("stream canceled")
        source = task.source_for_segment(segment_index, attempt)
        headers = dict(source.headers)
        headers.pop("Range", None)
        headers["Range"] = f"bytes={start + offset}-{end + offset}"
        headers.setdefault("Connection", "keep-alive")
        try:
            response = get(
                source.url,
                headers=headers,
                timeout=_UPSTREAM_TIMEOUT,
                follow_redirects=True,
            )
            if response.status_code != 206:
                raise UpstreamFetchError(
                    f"range request not honored: {response.status_code}"
                )
            payload = bytes(response.content or b"")
            if len(payload) != expected_length:
                raise UpstreamFetchError(
                    f"short segment body: {len(payload)} != {expected_length}"
                )
            return payload
        except (httpx.HTTPError, UpstreamFetchError) as exc:
            last_error = exc
            if attempt < max(1, retry_count) - 1:
                time.sleep(0.1 * (attempt + 1))
    raise UpstreamFetchError(f"segment fetch failed: {last_error}")


class _OrderedSegmentWriter:
    """按序重组分片:乱序完成的分片先入 pending,连续前缀立即回写播放器。"""

    def __init__(self, write) -> None:
        self._write = write
        self._pending: dict[int, bytes] = {}
        self._expected = -1
        self.bytes_written = 0

    def start(self, range_start: int) -> None:
        self._expected = range_start

    def accept(self, start: int, data: bytes) -> int:
        self._pending[start] = data
        written = 0
        while self._expected in self._pending:
            chunk = self._pending.pop(self._expected)
            self._write(chunk)
            written += len(chunk)
            self._expected += len(chunk)
        self.bytes_written += written
        return written

    @property
    def expected_offset(self) -> int:
        return self._expected


def serve_parallel_range(
    task: RangeProxyTask,
    range_start: int,
    range_end: int,
    write,
    *,
    get=httpx.get,
    stop_event: threading.Event | None = None,
) -> None:
    """并行分片服务一个 Range 请求。

    分片重试耗尽:尚未写出任何字节时抛 UpstreamFetchError(调用方可整体降级顺序重发),
    已部分写出时抛 StreamCommittedError(只能断流)。客户端断开抛 ClientDisconnectError。
    """
    probe = task.probe
    assert probe is not None
    total_length = probe.total_length
    segments = create_segments(range_start, range_end, task.chunk_size, total_length)
    worker_count = max(1, min(task.concurrency, len(segments)))
    prefetch_window_bytes = task.chunk_size * DEFAULT_PREFETCH_SEGMENTS
    stop = stop_event if stop_event is not None else threading.Event()
    writer = _OrderedSegmentWriter(write)
    writer.start(range_start)

    def fetch_one(
        segment_index: int, segment_start: int, segment_end: int
    ) -> tuple[int, bytes]:
        return segment_start, _fetch_segment(
            task,
            segment_index,
            segment_start,
            segment_end,
            get=get,
            stop_event=stop,
        )

    executor = ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix=f"range-proxy-{task.task_id}"
    )
    futures: set[Future] = set()
    submitted = 0
    completed = 0
    in_flight = 0
    failure: Exception | None = None
    try:
        while completed < len(segments):
            if stop.is_set():
                return
            while (
                not stop.is_set()
                and submitted < len(segments)
                and in_flight < worker_count
                and segments[submitted][0] - writer.expected_offset
                < prefetch_window_bytes
            ):
                segment_index = submitted
                segment_start, segment_end = segments[submitted]
                submitted += 1
                in_flight += 1
                futures.add(
                    executor.submit(
                        fetch_one, segment_index, segment_start, segment_end
                    )
                )
            done, futures = wait(
                futures, timeout=_COMPLETION_POLL_SECONDS, return_when=FIRST_COMPLETED
            )
            if not done:
                continue
            for future in done:
                in_flight -= 1
                completed += 1
                try:
                    segment_start, payload = future.result()
                except Exception as exc:  # noqa: BLE001 - 统一转成降级信号
                    failure = exc
                    break
                try:
                    writer.accept(segment_start, payload)
                except Exception as exc:
                    if _is_client_disconnect_error(exc):
                        raise ClientDisconnectError from exc
                    raise
            if failure is not None:
                break
    finally:
        stop.set()
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    if failure is not None:
        task.record_parallel_failure()
        if writer.bytes_written == 0:
            raise UpstreamFetchError(f"parallel fetch failed: {failure}") from failure
        raise StreamCommittedError(
            f"parallel stream interrupted: {failure}"
        ) from failure


def _is_client_disconnect_error(exc: BaseException) -> bool:
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in {errno.EPIPE, errno.ECONNRESET}
    return False


def open_sequential_stream(
    task: RangeProxyTask,
    range_start: int,
    range_end: int | None,
    *,
    stream=httpx.stream,
):
    """打开上游顺序流(Range 已按探测偏移平移);返回 httpx 响应,调用方负责关闭。

    上游忽略 Range(start>0 却回 200)时抛 UpstreamFetchError,避免发错偏移数据。
    """
    probe = task.probe
    offset = probe.stream_offset if probe else 0
    headers = dict(task.headers)
    headers.pop("Range", None)
    headers.pop("Host", None)
    if range_end is None:
        headers["Range"] = f"bytes={range_start + offset}-"
    else:
        headers["Range"] = f"bytes={range_start + offset}-{range_end + offset}"
    headers.setdefault("Connection", "keep-alive")
    response = stream(
        "GET",
        task.url,
        headers=headers,
        timeout=_UPSTREAM_TIMEOUT,
        follow_redirects=True,
    )
    upstream_start = range_start + offset
    if upstream_start > 0 and response.status_code == 200:
        response.close()
        raise UpstreamFetchError("upstream ignored Range header")
    return response, upstream_start


def serve_sequential_stream(
    task: RangeProxyTask,
    range_start: int,
    range_end: int | None,
    write,
    *,
    stream=httpx.stream,
    stop_event: threading.Event | None = None,
) -> None:
    """顺序流式透传:上游不支持 Range 或并行连续失败时的降级路径。"""
    with open_sequential_stream(task, range_start, range_end, stream=stream)[
        0
    ] as response:
        response.raise_for_status()
        for chunk in response.iter_bytes(chunk_size=SEQUENTIAL_STREAM_CHUNK_SIZE):
            if stop_event is not None and stop_event.is_set():
                return
            if chunk:
                write(chunk)


__all__ = [
    "ClientDisconnectError",
    "DRIVE_PROXY_RULES",
    "RangeProxyProbe",
    "RangeProxyRegistry",
    "RangeProxySource",
    "RangeProxyTask",
    "StreamCommittedError",
    "UpstreamFetchError",
    "create_segments",
    "compute_initial_segment_size",
    "ensure_probed",
    "normalize_drive_type",
    "open_sequential_stream",
    "parse_drive_type_from_url",
    "parse_range_header",
    "probe_task",
    "resolve_proxy_rule",
    "serve_parallel_range",
    "serve_sequential_stream",
]
