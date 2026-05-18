from __future__ import annotations

import threading
from collections.abc import Callable

import httpx
import requests

from atv_player.network_proxy import ProxyDecider


_DEFAULT_POOL_LIMIT = 10
_MANUAL_PREFIX = "manual:"


def _client_key(decider: ProxyDecider | None, url: str) -> str:
    if decider is None:
        return "direct"
    decision = decider.decide(url)
    if decision.kind == "direct":
        return "direct"
    if decision.kind == "system":
        return "system"
    if decision.kind == "manual":
        return f"{_MANUAL_PREFIX}{decision.proxy_url}"
    return "direct"


def _default_client_factory(key: str, pool_limit: int) -> httpx.Client:
    limits = httpx.Limits(
        max_connections=pool_limit,
        max_keepalive_connections=pool_limit,
    )
    if key == "direct":
        return httpx.Client(trust_env=False, limits=limits)
    if key == "system":
        return httpx.Client(trust_env=True, limits=limits)
    if key.startswith(_MANUAL_PREFIX):
        proxy_url = key[len(_MANUAL_PREFIX):]
        return httpx.Client(proxy=proxy_url, trust_env=False, limits=limits)
    raise ValueError(f"unknown client key: {key}")


def _default_session_factory(pool_limit: int) -> requests.Session:
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=pool_limit,
        pool_maxsize=pool_limit,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class NetworkClient:
    def __init__(
        self,
        decider_factory: Callable[[], ProxyDecider | None],
        *,
        pool_limit: int = _DEFAULT_POOL_LIMIT,
        client_factory: Callable[[str, int], httpx.Client] = _default_client_factory,
        session_factory: Callable[[int], requests.Session] = _default_session_factory,
    ) -> None:
        self._decider_factory = decider_factory
        self._pool_limit = pool_limit
        self._client_factory = client_factory
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._decider: ProxyDecider | None = None
        self._decider_loaded = False
        self._clients: dict[str, httpx.Client] = {}
        self._session: requests.Session | None = None
        self._closed = False

    @property
    def proxy_decider(self) -> ProxyDecider | None:
        with self._lock:
            return self._load_decider_locked()

    def _load_decider_locked(self) -> ProxyDecider | None:
        if not self._decider_loaded:
            self._decider = self._decider_factory()
            self._decider_loaded = True
        return self._decider

    def invalidate_proxy(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._decider = None
            self._decider_loaded = False
            non_direct_keys = [k for k in self._clients if k != "direct"]
            clients_to_close = [self._clients.pop(k) for k in non_direct_keys]
            session_to_close = self._session
            self._session = None
        for client in clients_to_close:
            try:
                client.close()
            except Exception:
                pass
        if session_to_close is not None:
            try:
                session_to_close.close()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            clients_to_close = list(self._clients.values())
            self._clients.clear()
            session_to_close = self._session
            self._session = None
            self._decider = None
            self._decider_loaded = False
        for client in clients_to_close:
            try:
                client.close()
            except Exception:
                pass
        if session_to_close is not None:
            try:
                session_to_close.close()
            except Exception:
                pass

    def _resolve(self, url: str) -> httpx.Client:
        with self._lock:
            if self._closed:
                raise RuntimeError("NetworkClient is closed")
            decider = self._load_decider_locked()
            key = _client_key(decider, url)
            client = self._clients.get(key)
            if client is None:
                client = self._client_factory(key, self._pool_limit)
                self._clients[key] = client
            return client

    def get(self, url: str, **kwargs):
        return self._resolve(url).get(url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._resolve(url).post(url, **kwargs)

    def stream(self, method: str, url: str, **kwargs):
        return self._resolve(url).stream(method, url, **kwargs)

    def requests_session(self) -> requests.Session:
        with self._lock:
            if self._closed:
                raise RuntimeError("NetworkClient is closed")
            if self._session is None:
                self._session = self._session_factory(self._pool_limit)
            return self._session
