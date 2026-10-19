import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from atv_player.models import PlayItem
from atv_player.proxy.range_proxy import (
    RangeProxyProbe,
    RangeProxySource,
    RangeProxyTask,
    UpstreamFetchError,
    create_segments,
    ensure_probed,
    normalize_drive_type,
    parse_drive_type_from_url,
    parse_range_header,
    probe_task,
    resolve_proxy_rule,
    sanitize_upstream_headers,
    serve_parallel_range,
)
from atv_player.proxy.server import LocalHlsProxyServer
from atv_player.ui.player_window import PlayerWindow


class FakeUpstreamResponse:
    def __init__(self, status_code, headers, content):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise UpstreamFetchError(f"upstream status {self.status_code}")


def test_parse_range_header_variants() -> None:
    assert parse_range_header("bytes=10-19", 100) == (10, 19)
    assert parse_range_header("bytes=90-", 100) == (90, 99)
    assert parse_range_header("bytes=-10", 100) == (90, 99)
    assert parse_range_header("bytes=95-", 90) is None
    assert parse_range_header("bytes=50-40", 100) is None
    assert parse_range_header("junk", 100) is None


def test_create_segments_quick_start_and_coverage() -> None:
    # 256KB 分片不触发大首片路径:1MB 文件用 32KB 快速启动首片,降低起播延迟。
    segments = create_segments(0, 1024 * 1024 - 1, 256 * 1024, 1024 * 1024)
    assert segments[0] == (0, 32 * 1024 - 1)
    assert segments[-1][1] == 1024 * 1024 - 1
    cursor = 0
    for start, end in segments:
        assert start == cursor
        cursor = end + 1
    assert cursor == 1024 * 1024

    # 大分片(>1MB)走 LARGE_FIRST_CHUNK_CAP 路径:首片即分片大小。
    large = create_segments(0, 4 * 1024 * 1024 - 1, 2 * 1024 * 1024, 4 * 1024 * 1024)
    assert large[0] == (0, 2 * 1024 * 1024 - 1)

    small = create_segments(0, 100, 256 * 1024, 101)
    assert small == [(0, 100)]


def test_drive_type_rules() -> None:
    assert normalize_drive_type("PAN115") == "pan115"
    assert parse_drive_type_from_url("https://cdn-cf.115.com/file") == "pan115"
    assert resolve_proxy_rule("quark") == (20, 1024 * 1024)
    assert resolve_proxy_rule("unknown") == (0, 0)


def test_sanitize_upstream_headers_strips_ows() -> None:
    # 后端 UC/夸克直链的 Referer 故意带「URL+"\ "」尾巴绕 checkplay 回调;
    # h11 拒绝尾部空白,剥掉后反斜杠保留(绕过语义不变)。
    url = "https://dl-uf-zb.pds.uc.cn/file?Signature=x"
    assert sanitize_upstream_headers({"Referer": url + "\\ "}) == {"Referer": url + "\\"}
    assert sanitize_upstream_headers({" User-Agent ": " ua "}) == {"User-Agent": "ua"}
    assert sanitize_upstream_headers({"": "x"}) == {}
    assert sanitize_upstream_headers(None) == {}


def test_range_proxy_task_sanitizes_task_and_source_headers() -> None:
    url = "https://up.example/file"
    sources = [
        RangeProxySource(url, {"Referer": url + "\\ "}),
        RangeProxySource(url + "2", {"Cookie": " b=2\t"}),
    ]
    task = RangeProxyTask(
        "t1",
        url,
        {"Referer": url + "\\ ", "User-Agent": " ua "},
        drive_type="uc",
        sources=sources,
    )

    assert task.headers["Referer"] == url + "\\"
    assert task.headers["User-Agent"] == "ua"
    assert [s.headers for s in task.sources] == [
        {"Referer": url + "\\"},
        {"Cookie": "b=2"},
    ]
    # 无 sources 时分片请求复用 task.headers,同样已消毒。
    single = RangeProxyTask("t2", url, {"Referer": url + "\\ "}, drive_type="uc")
    assert single.source_for_segment(0, 0).headers["Referer"] == url + "\\"


def test_probe_task_detects_range_and_total() -> None:
    seen: list[dict] = []

    def fake_get(url, *, headers=None, timeout=None, follow_redirects=True):
        seen.append(dict(headers or {}))
        return FakeUpstreamResponse(
            206,
            {"Content-Range": "bytes 0-2047/987654321", "Content-Type": "video/mp4"},
            b"\x00" * 2048,
        )

    task = RangeProxyTask(
        "t1", "https://up.example/file", {"Cookie": "a=1"}, drive_type="ali"
    )
    probe = probe_task(task, get=fake_get)

    assert probe.supports_range is True
    assert probe.total_length == 987654321
    assert probe.stream_offset == 0
    assert task.state == "ready_parallel"
    assert seen[0]["Range"] == "bytes=0-2047"
    assert seen[0]["Cookie"] == "a=1"


def test_probe_task_without_range_support_is_sequential() -> None:
    def fake_get(url, *, headers=None, timeout=None, follow_redirects=True):
        return FakeUpstreamResponse(
            200, {"Content-Length": "4096", "Content-Type": "video/mp4"}, b"\x00" * 4096
        )

    task = RangeProxyTask("t1", "https://up.example/file", {}, drive_type="ali")
    probe = probe_task(task, get=fake_get)

    assert probe.supports_range is False
    assert task.state == "ready_sequential"


def test_probe_task_detects_obfuscated_matroska() -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"\x1a\x45\xdf\xa3" + b"\x00" * 100

    def fake_get(url, *, headers=None, timeout=None, follow_redirects=True):
        return FakeUpstreamResponse(
            206, {"Content-Range": f"bytes 0-2047/{len(payload)}"}, payload
        )

    task = RangeProxyTask("t1", "https://up.example/file", {}, drive_type="ali")
    probe = ensure_probed(task, get=fake_get)

    assert probe.stream_offset == 8
    assert probe.content_type == "video/x-matroska"


def test_serve_parallel_range_reorders_out_of_order_segments() -> None:
    total = 64 * 1024
    chunk = 16 * 1024
    body = bytes(range(256)) * (total // 256)
    requested_ranges: list[str] = []

    def fake_get(url, *, headers=None, timeout=None, follow_redirects=True):
        range_header = headers["Range"]
        requested_ranges.append(range_header)
        start_text, end_text = range_header.removeprefix("bytes=").split("-")
        start, end = int(start_text), int(end_text)
        # 低偏移分片人为放慢,迫使乱序完成,验证按序重组。
        if start == 0:
            time.sleep(0.15)
        return FakeUpstreamResponse(
            206,
            {"Content-Range": f"bytes {start}-{end}/{total}"},
            body[start : end + 1],
        )

    task = RangeProxyTask(
        "t1",
        "https://up.example/file",
        {},
        drive_type="ali",
        concurrency=4,
        chunk_size=chunk,
    )
    task.probe = RangeProxyProbe(
        supports_range=True, total_length=total, content_type="video/mp4"
    )
    written = bytearray()

    serve_parallel_range(task, 0, total - 1, written.extend, get=fake_get)

    assert bytes(written) == body
    assert len(requested_ranges) == total // chunk


def test_serve_parallel_range_rotates_multi_sources() -> None:
    total = 32 * 1024
    chunk = 8 * 1024
    body = b"\x11" * total
    urls_seen: set[str] = set()

    def fake_get(url, *, headers=None, timeout=None, follow_redirects=True):
        urls_seen.add(url)
        start_text, end_text = headers["Range"].removeprefix("bytes=").split("-")
        start, end = int(start_text), int(end_text)
        return FakeUpstreamResponse(
            206,
            {"Content-Range": f"bytes {start}-{end}/{total}"},
            body[start : end + 1],
        )

    task = RangeProxyTask(
        "t1",
        "https://primary.example/file",
        {},
        drive_type="ali",
        concurrency=4,
        chunk_size=chunk,
        sources=[
            RangeProxySource("https://a.example/file", {"Cookie": "a"}),
            RangeProxySource("https://b.example/file", {"Cookie": "b"}),
        ],
    )
    task.probe = RangeProxyProbe(
        supports_range=True, total_length=total, content_type="video/mp4"
    )

    written = bytearray()
    serve_parallel_range(task, 0, total - 1, written.extend, get=fake_get)

    assert bytes(written) == body
    assert urls_seen == {"https://a.example/file", "https://b.example/file"}


def test_serve_parallel_range_raises_when_range_not_honored() -> None:
    total = 8 * 1024

    def fake_get(url, *, headers=None, timeout=None, follow_redirects=True):
        # 故意忽略 Range,始终返回 200 全量内容。
        return FakeUpstreamResponse(
            200, {"Content-Length": str(total)}, b"\x00" * total
        )

    task = RangeProxyTask(
        "t1",
        "https://up.example/file",
        {},
        drive_type="ali",
        concurrency=2,
        chunk_size=4096,
    )
    task.probe = RangeProxyProbe(
        supports_range=True, total_length=total, content_type="video/mp4"
    )

    with pytest.raises(UpstreamFetchError):
        serve_parallel_range(task, 0, total - 1, lambda chunk: None, get=fake_get)


def _build_upstream(body: bytes, *, fail_ranges: set[str] | None = None):
    fail_ranges = fail_ranges or set()
    lock = threading.Lock()

    def fake_get(url, *, headers=None, timeout=None, follow_redirects=True):
        range_header = headers.get("Range", "")
        with lock:
            if range_header in fail_ranges:
                return FakeUpstreamResponse(403, {}, b"")
        if (
            not range_header
            or range_header == "bytes=0-"
            or range_header == f"bytes=0-{len(body) - 1}"
        ):
            return FakeUpstreamResponse(200, {"Content-Length": str(len(body))}, body)
        start_text, end_text = range_header.removeprefix("bytes=").split("-")
        start, end = int(start_text), int(end_text)
        return FakeUpstreamResponse(
            206,
            {"Content-Range": f"bytes {start}-{end}/{len(body)}"},
            body[start : end + 1],
        )

    return fake_get


def test_local_proxy_range_proxy_end_to_end_parallel() -> None:
    total = 512 * 1024
    # 长周期模式:重复写首块等错位 corruption 一定会导致逐字节比对失败。
    body = (bytes(range(256)) * 64 + b"ATV-MARKER") * (total // (256 * 64 + 10))
    total = len(body)
    requested: list[str] = []
    base_fake = _build_upstream(body)

    def fake_get(url, *, headers=None, timeout=None, follow_redirects=True):
        range_header = (headers or {}).get("Range", "")
        if range_header != "bytes=0-2047":
            requested.append(range_header)
        return base_fake(
            url, headers=headers, timeout=timeout, follow_redirects=follow_redirects
        )

    server = LocalHlsProxyServer(port=0, get=fake_get)
    server.start()
    try:
        local_url = server.create_range_proxy_url(
            "https://up.example/movie.bin",
            {"Cookie": "sid=1", "User-Agent": "atv-test"},
            drive_type="ali",
            chunk_size=64 * 1024,
        )
        assert local_url is not None

        # 跨多个分片(32KB 快速启动首片 + 64KB 分片)逐字节比对:能抓住首块重复写等错位。
        request = Request(local_url, headers={"Range": f"bytes=0-{200 * 1024 - 1}"})
        with urlopen(request, timeout=15) as response:
            assert response.status == 206
            assert (
                response.headers.get("Content-Range")
                == f"bytes 0-{200 * 1024 - 1}/{total}"
            )
            payload = response.read()
        assert payload == body[: 200 * 1024]
        # 并行分片确实打到了上游
        assert len(requested) >= 3, "upstream should have received parallel range requests"

        # 中段开区间(播放器 seek 场景)
        middle = 300 * 1024
        with urlopen(
            Request(local_url, headers={"Range": f"bytes={middle}-"}), timeout=15
        ) as response:
            assert response.status == 206
            assert response.read() == body[middle:]

        # 无 Range 请求返回完整内容
        with urlopen(local_url, timeout=10) as response:
            assert response.status == 200
            assert response.read() == body

        # HEAD 返回头部
        head_request = Request(
            local_url, method="HEAD", headers={"Range": "bytes=0-1023"}
        )
        with urlopen(head_request, timeout=10) as response:
            assert response.status == 206
            assert response.headers.get("Content-Length") == "1024"

        # 未知任务 404
        with pytest.raises(HTTPError) as excinfo:
            urlopen(local_url.rsplit("/", 1)[0] + "/missing", timeout=10)
        assert excinfo.value.code == 404
    finally:
        server.close()


def test_local_proxy_range_proxy_falls_back_to_sequential() -> None:
    total = 256 * 1024
    body = b"\x22" * total
    # 仅探测范围(0-2047)与整文件请求可用,其余 Range 一律 403 → 并行必失败。
    fake_get = _build_upstream(body, fail_ranges={"bytes=2048-65535"})

    server = LocalHlsProxyServer(port=0, get=fake_get)
    server.start()
    try:
        local_url = server.create_range_proxy_url(
            "https://up.example/movie.bin",
            {},
            drive_type="ali",
            chunk_size=64 * 1024,
        )
        assert local_url is not None

        # mpv 首个请求通常带 bytes=0-:并行取 0-65535 失败(403),应整体降级顺序流式重发。
        request = Request(local_url, headers={"Range": "bytes=0-"})
        with urlopen(request, timeout=15) as response:
            payload = response.read()
        assert payload == body
    finally:
        server.close()


def _make_stub_window(loader, proxy_server, session):
    stub = PlayerWindow.__new__(PlayerWindow)
    stub._drive_link_loader = loader
    stub._m3u8_ad_filter = SimpleNamespace(proxy_server=proxy_server)
    stub.session = session
    return stub


def _drive_item(path: str, url: str) -> PlayItem:
    return PlayItem(title="第1集", url=url, path=path, play_id="1@23")


def test_prepare_drive_parallel_url_registers_task() -> None:
    captured: dict = {}

    def fake_create(
        url,
        headers,
        *,
        drive_type="",
        concurrency=0,
        chunk_size=0,
        sources=None,
        task_id="",
        file_name="",
        content_type="",
    ):
        captured.update(
            url=url,
            headers=headers,
            drive_type=drive_type,
            sources=sources,
            file_name=file_name,
        )
        return f"http://127.0.0.1:2323/driver/{task_id}"

    loader_calls: list[tuple[str, str]] = []

    def fake_loader(resource_id, path):
        loader_calls.append((resource_id, path))
        return {
            "url": "https://dl.quark.example/file?sign=x",
            "header": {"Cookie": "qk=1", "User-Agent": "Quark/1.0"},
            "type": "QUARK",
            "name": "第1集.mkv",
            "multiUrls": [
                {"url": "https://a.example/file", "header": {"Cookie": "a"}},
                {"url": "https://b.example/file", "header": {"Cookie": "b"}},
            ],
        }

    session = SimpleNamespace(drive_resource_id="c2l0ZQ==")
    stub = _make_stub_window(
        fake_loader, SimpleNamespace(create_range_proxy_url=fake_create), session
    )
    item = _drive_item(
        "/temp/quark@share@code/第1集.mkv", "http://192.168.50.60:4567/p/tok/1@23"
    )

    local_url = PlayerWindow._prepare_drive_parallel_url(stub, item, item.url)

    assert local_url.startswith("http://127.0.0.1:2323/driver/")
    assert loader_calls == [("c2l0ZQ==", "/temp/quark@share@code/第1集.mkv")]
    assert captured["url"] == "https://dl.quark.example/file?sign=x"
    assert captured["headers"] == {"Cookie": "qk=1", "User-Agent": "Quark/1.0"}
    assert captured["drive_type"] == "quark"
    assert captured["file_name"] == "第1集.mkv"
    assert captured["sources"] is not None and len(captured["sources"]) == 2


def test_prepare_drive_parallel_url_fallbacks() -> None:
    session = SimpleNamespace(drive_resource_id="")

    # 非后端代理地址不处理
    stub = _make_stub_window(
        lambda resource_id, path: pytest.fail("loader should not be called"),
        SimpleNamespace(create_range_proxy_url=lambda *a, **k: "x"),
        session,
    )
    direct = _drive_item("/temp/quark@s@c/a.mkv", "https://dl.quark.example/a.mkv")
    assert PlayerWindow._prepare_drive_parallel_url(stub, direct, direct.url) == ""

    # ISO 保持原 ISO 检查流程
    iso = _drive_item("/temp/quark@s@c/movie.iso", "http://backend:4567/p/tok/1@23.iso")
    assert PlayerWindow._prepare_drive_parallel_url(stub, iso, iso.url) == ""

    # path 缺失(如 msub 解析结果)回退
    no_path = PlayItem(title="x", url="http://backend:4567/p/tok/1@23", path="")
    assert PlayerWindow._prepare_drive_parallel_url(stub, no_path, no_path.url) == ""


def test_prepare_drive_parallel_url_survives_loader_error() -> None:
    def broken_loader(resource_id, path):
        raise RuntimeError("server 404: legacy backend")

    session = SimpleNamespace(drive_resource_id="")
    stub = _make_stub_window(
        broken_loader,
        SimpleNamespace(create_range_proxy_url=lambda *a, **k: "x"),
        session,
    )
    item = _drive_item("/temp/quark@s@c/a.mkv", "http://backend:4567/p/tok/1@23")

    assert PlayerWindow._prepare_drive_parallel_url(stub, item, item.url) == ""


def test_prepare_drive_parallel_url_strips_markers() -> None:
    captured: dict = {}

    def fake_create(url, headers, **kwargs):
        captured["url"] = url
        return "http://127.0.0.1:2323/driver/t"

    def fake_loader(resource_id, path):
        return {"url": "https://dl.example/file#proxy=0", "header": {}, "type": "ALI"}

    session = SimpleNamespace(drive_resource_id="")
    stub = _make_stub_window(
        fake_loader, SimpleNamespace(create_range_proxy_url=fake_create), session
    )
    item = _drive_item("/temp/ali@s@c/a.mp4", "http://backend:4567/p/tok/1@23")

    assert PlayerWindow._prepare_drive_parallel_url(stub, item, item.url)
    assert captured["url"] == "https://dl.example/file"
