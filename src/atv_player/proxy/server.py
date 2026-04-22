from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import errno
import logging
import threading
from urllib.parse import parse_qs, quote, urlparse

import httpx

from atv_player.proxy.m3u8 import rewrite_playlist
from atv_player.proxy.segment import SegmentProxy
from atv_player.proxy.session import ProxySessionRegistry

logger = logging.getLogger(__name__)


class LocalHlsProxyServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 2323, get=httpx.get) -> None:
        self.host = host
        self.port = port
        self._preferred_port = port
        self._get = get
        self._registry = ProxySessionRegistry()
        self._segment_proxy = SegmentProxy(self._registry, get=get)
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

    def create_playlist_url(self, url: str, headers: dict[str, str] | None = None) -> str:
        token = self._registry.create_session(url, dict(headers or {}))
        return f"http://{self.host}:{self.port}/m3u?token={quote(token)}"

    def handle_request(self, method: str, path: str) -> tuple[int, list[tuple[str, str]], bytes]:
        parsed = urlparse(path)
        query = parse_qs(parsed.query)
        try:
            if method != "GET":
                return 405, [], b"method not allowed"
            if parsed.path == "/m3u":
                token = query["token"][0]
                session = self._registry.get(token)
                if session is None:
                    return 404, [], b"missing proxy session"
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
                token = query["token"][0]
                session = self._registry.get(token)
                if session is None:
                    return 404, [], b"missing proxy session"
                index = int(query["i"][0])
                payload = self._segment_proxy.fetch_segment(token, index)
                return 200, [("Content-Type", "video/MP2T")], payload
            if parsed.path == "/asset":
                token = query["token"][0]
                session = self._registry.get(token)
                if session is None:
                    return 404, [], b"missing proxy session"
                asset_url = query["url"][0]
                payload = self._segment_proxy.fetch_asset(token, asset_url)
                return 200, [], payload
        except Exception as exc:
            return 502, [], str(exc).encode("utf-8")
        return 404, [], b"not found"

    def _handler_type(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                status, headers, payload = parent.handle_request("GET", self.path)
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args) -> None:
                return None

        return Handler
